"""Bounded PNG/JPEG accounting for explicitly approved native image inputs.

This is not a decoder, file server, image transformer or model router. Only
byte-identical coordinator-selected files may enter the native provider body.
"""
from __future__ import annotations
import base64
import binascii
import copy
import hashlib
import io
import struct
import zlib
from pathlib import Path
from vkm_worker_core import need, json_bytes, verify_file_record, inside, relative_file, WorkerError

RULES = {
    'anthropic/claude-opus-5.5': ('google-vertex/global', 'CLAUDE_HIGHRES_MAX4784'),
    'google/gemini-4-hydrogen': ('google-ai-studio/flex', 'GEMINI4_MAX2240'),
}
# Charged in addition to the visual-token upper limit and the existing global
# UTF8/protocol guard. This is a safety allowance, not measured image overhead.
IMAGE_PROTOCOL_GUARD = 256


def dimensions(data: bytes, mime: str) -> tuple[int, int]:
    if mime == 'image/png':
        need(len(data) >= 33 and data[:8] == b'\x89PNG\r\n\x1a\n', 'invalid PNG signature')
        need(data[8:16] == b'\0\0\0\rIHDR', 'invalid PNG IHDR')
        need(zlib.crc32(data[12:29]) & 0xffffffff == struct.unpack('>I',data[29:33])[0], 'invalid PNG IHDR CRC')
        width,height=struct.unpack('>II',data[16:24])
    else:
        need(mime == 'image/jpeg' and data.startswith(b'\xff\xd8'), 'only PNG/JPEG admitted')
        offset=2; width=height=0
        while offset < len(data):
            need(data[offset] == 255,'invalid JPEG marker'); offset+=1
            while offset < len(data) and data[offset] == 255: offset+=1
            need(offset < len(data),'truncated JPEG marker')
            marker=data[offset];offset+=1
            need(marker not in {0,0xda,0xd9},'JPEG dimensions missing before scan')
            if marker in {0xd8,0x01,*range(0xd0,0xd8)}: continue
            need(offset+2<=len(data),'truncated JPEG length')
            size=int.from_bytes(data[offset:offset+2],'big')
            need(size>=2 and offset+size<=len(data),'invalid JPEG segment')
            if marker in {0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf}:
                need(size>=8,'short JPEG SOF')
                height,width=struct.unpack('>HH',data[offset+3:offset+7]);break
            offset+=size
        need(width and height,'JPEG frame missing')
    need(width>0 and height>0,'invalid image dimensions')
    return width,height


class ImageAdmission:
    def __init__(self, policy: dict, profile: dict, *, workspace: Path | None = None,
                 read_paths: list[str] | None = None, task_id: str | None = None, metadata_guard=None):
        need(isinstance(policy,dict) and set(policy)=={'schema','status','task_id','model_id','provider_only','rule','sources','proofs','decoder'},'invalid image admission contract')
        need(policy['schema']=='vkm.opencode.image-admission/1' and policy['status']=='APPROVED_BY_MAIN_CODEX','image admission awaits main review')
        model=profile['model_id'];need(model in RULES,'image accounting unavailable for selected model')
        provider,rule=RULES[model]
        need(profile['provider_only']==[provider] and policy['model_id']==model and policy['provider_only']==[provider] and policy['rule']==rule,'image model/provider/rule differs')
        need(isinstance(policy['task_id'],str) and policy['task_id'] and (task_id is None or task_id==policy['task_id']),'image task binding differs')
        need(isinstance(policy['proofs'],list) and policy['proofs'],'image qualification proofs missing')
        for proof in policy['proofs']: verify_file_record(proof)
        decoder=policy['decoder']
        need(isinstance(decoder,dict) and set(decoder)=={'version','files'} and isinstance(decoder['version'],str) and isinstance(decoder['files'],list) and decoder['files'],'decoder pins required')
        if workspace is not None:
            import PIL
            from PIL import Image,PngImagePlugin,JpegImagePlugin
            need(PIL.__version__==decoder['version'],'image decoder version changed')
            expected={str(Path(x.__file__).resolve()) for x in [PIL,Image,PngImagePlugin,JpegImagePlugin,Image.core]}
            need({str(Path(x.get('path','')).resolve()) for x in decoder['files']}==expected,'image decoder pin set incomplete')
            for record in decoder['files']: verify_file_record(record)
            need(callable(metadata_guard),'pinned Delegate metadata guard required')
        sources=policy['sources'];need(isinstance(sources,list) and sources,'image sources missing')
        self.allowed={};self.model=model;self.rule=rule
        for source in sources:
            need(isinstance(source,dict) and set(source)=={'path','sha256','mime','width','height'},'invalid image source record')
            path=relative_file(source['path'],read_only=True)
            need(source['mime'] in {'image/png','image/jpeg'},'unqualified image format')
            need(type(source['width']) is int and type(source['height']) is int and source['width']>0 and source['height']>0,'invalid approved dimensions')
            sha=source['sha256'];need(isinstance(sha,str) and len(sha)==64 and all(c in '0123456789abcdef' for c in sha),'invalid image digest')
            if sha in self.allowed:
                need({k:source[k] for k in ['mime','width','height']}=={k:self.allowed[sha][k] for k in ['mime','width','height']},'same SHA rebound to different image metadata')
            self.allowed[sha]=source
            if workspace is not None:
                need(read_paths is not None and path in read_paths,'image source outside exact read paths')
                target=workspace/path
                inside(target,workspace)
                data=verify_file_record({'path':str(target),'sha256':sha}).read_bytes()
                need(dimensions(data,source['mime'])==(source['width'],source['height']),'approved image dimensions do not match bytes')
                # Reuse the installed, version-pinned decoder and existing
                # Delegate secret/PNG-metadata guard. Original bytes are retained.
                from PIL import Image
                need(source['width']*source['height']*8<=256*1024*1024,'image decoder RAM guard')
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        need(Image.MIME.get(image.format)==source['mime'] and image.size==(source['width'],source['height']) and getattr(image,'n_frames',1)==1,'decoded image format/pixels/frames differ')
                        image.verify()
                    with Image.open(io.BytesIO(data)) as image:
                        image.load();metadata_guard(image,None,v2=True)
                except Exception:
                    raise WorkerError('invalid image pixels/format or forbidden metadata') from None

    def body_bound(self, body: dict) -> tuple[int, dict]:
        safe=copy.deepcopy(body);images=[]
        for message in safe['messages']:
            need(isinstance(message,dict),'invalid message')
            content=message.get('content')
            if not isinstance(content,list):
                need(content is None or isinstance(content,str),'unqualified content');continue
            for index,part in enumerate(content):
                need(isinstance(part,dict),'invalid content item')
                if part.get('type')=='text':
                    need(isinstance(part.get('text'),str),'invalid text item');continue
                need(set(part)=={'type','image_url'} and part['type']=='image_url','unqualified nontext accounting')
                value=part['image_url']
                need(isinstance(value,dict) and set(value) in ({'url'},{'url','detail'}) and value.get('detail','auto')=='auto','unqualified image detail/options')
                uri=value['url'];need(isinstance(uri,str) and uri.startswith(('data:image/png;base64,','data:image/jpeg;base64,')),'external image URL or format denied')
                header,encoded=uri.split(',',1);mime=header[5:-7]
                try:data=base64.b64decode(encoded,validate=True)
                except (ValueError,binascii.Error):raise WorkerError('invalid image base64') from None
                sha=hashlib.sha256(data).hexdigest();source=self.allowed.get(sha)
                need(source is not None,'image bytes absent from approved SHA allowlist')
                width,height=dimensions(data,mime)
                need(mime==source['mime'] and (width,height)==(source['width'],source['height']),'image MIME/pixel metadata rebound')
                if self.model=='anthropic/claude-opus-5.5':
                    need(max(width,height)<=8000,'Claude image dimension limit')
                    need(len(encoded)<=10*1024*1024,'Claude image exceeds documented encoded size')
                    # Documented native max covers padding, possible upscale and
                    # any unknown minimum floor; never use raw pixel count alone.
                    visual=4784
                else:
                    # Maximum documented resolution allocation (ultra_high),
                    # twice the default1120; independent of source resolution.
                    visual=2240
                images.append({'sha256':sha,'mime':mime,'width':width,'height':height,'visual_tokens_upper':visual,'protocol_guard_tokens':IMAGE_PROTOCOL_GUARD})
                content[index]={'type':'text','text':'[APPROVED_IMAGE_SHA256:'+sha+']'}
        if self.model=='anthropic/claude-opus-5.5' and len(images)>20:
            need(all(max(x['width'],x['height'])<=3000 for x in images),'Claude many-image dimension limit')
        text_bytes=len(json_bytes(safe));bound=text_bytes+4096+sum(x['visual_tokens_upper']+IMAGE_PROTOCOL_GUARD for x in images)
        return bound,{'rule':self.rule,'image_count':len(images),'images':images,'text_and_schema_bytes':text_bytes,'global_protocol_guard':4096,'image_base64_not_counted_as_text':True,
                      'upscale_padding_minimum_floor':'COVERED_BY_DOCUMENTED_NATIVE_MAX_PER_IMAGE','image_overhead':'ADDITIONAL256_TOKEN_SAFETY_GUARD_NOT_MEASURED','provider_token_count':'NOT_MEASURED'}
