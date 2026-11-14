"""Synthetic CV runtime checks; explicit paths, no scientific inputs or services."""
import argparse,contextlib,hashlib,importlib.util,json,os,pathlib,sys,time

def write(path,data):
    pathlib.Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

def synthetic(out):
    import numpy as np
    from PIL import Image,ImageDraw
    image=Image.new('RGB',(256,256),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((40,45,160,180),fill=(200,55,45),outline='black',width=2)
    draw.ellipse((180,175,235,230),fill=(40,85,210),outline='black')
    image.save(out/'synthetic.png')
    return np.asarray(image).copy()

def save_mask(out,image,mask):
    import numpy as np
    from PIL import Image
    mask=np.asarray(mask,dtype=bool); assert mask.shape==(256,256)
    assert mask.any() and not mask.all()
    Image.fromarray(mask.astype('uint8')*255).save(out/'mask.png')
    overlay=np.array(image).copy();overlay[mask]=(0.5*overlay[mask]+0.5*np.array([0,255,0])).astype('uint8')
    Image.fromarray(overlay).save(out/'overlay.png')
    return {'pixels':int(mask.sum()),'prompt_pixel_included':bool(mask[110,100])}

@contextlib.contextmanager
def gpu_lock(path):
    p=pathlib.Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    try:fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:raise RuntimeError('BLOCKED_GPU_BUSY: shared lock exists')
    try:
        os.write(fd,json.dumps({'pid':os.getpid(),'task':'VKM-ENV-01 CV smoke','time':time.time(),'platform':sys.platform}).encode());os.close(fd)
        yield
    finally:p.unlink()

def sam(a,out):
    import numpy as np,torch
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor
    image=synthetic(out)
    with gpu_lock(a.gpu_lock):
        assert torch.cuda.is_available()
        torch.set_num_threads(2)
        start=time.monotonic()
        model=build_sam2('configs/sam2.1/sam2.1_hiera_t.yaml',a.checkpoint,device='cuda',apply_postprocessing=False)
        predictor=SAM2ImagePredictor(model)
        with torch.inference_mode(),torch.autocast('cuda',dtype=torch.bfloat16):
            predictor.set_image(image)
            masks,scores,_=predictor.predict(point_coords=np.array([[100,110]]),point_labels=np.array([1]),multimask_output=True)
        torch.cuda.synchronize();mask=masks[int(np.argmax(scores))]
        stats=save_mask(out,image,mask)
        assert stats['prompt_pixel_included']
        result={'status':'PASS','model':'SAM2.1 Hiera Tiny','checkpoint_sha256':hashlib.sha256(pathlib.Path(a.checkpoint).read_bytes()).hexdigest(),'device':torch.cuda.get_device_name(0),'torch':torch.__version__,'cuda':torch.version.cuda,'capability':torch.cuda.get_device_capability(0),'max_vram_bytes':torch.cuda.max_memory_allocated(),'elapsed_s':time.monotonic()-start,'scores':[float(s) for s in scores],**stats,'optional_cuda_extension':'NOT_BUILT: SAM2_BUILD_CUDA=0; hole/sprinkle postprocessing explicitly disabled; model inference uses CUDA'}
    return result

def xany(a,out):
    import numpy as np,onnxruntime as ort
    from PyQt6.QtWidgets import QApplication
    from anylabeling.views.labeling.label_file import LabelFile
    from anylabeling.services.auto_labeling.__base__.sam2 import SegmentAnything2ONNX
    from anylabeling import app_info
    app=QApplication.instance() or QApplication([])
    image=synthetic(out);path=out/'synthetic.json'
    shape={'label':'SYNTHETIC_AREA','points':[[40,45],[160,45],[160,180],[40,180]],'group_id':None,'shape_type':'polygon','flags':{},'description':'synthetic stable_id AREA1; frame SYNTHETIC_PIXEL; px'}
    LabelFile().save(filename=str(path),shapes=[shape],image_path='synthetic.png',image_height=256,image_width=256,image_data=None,flags={},other_data={'fixture':'VKM-ENV-01','frame_id':'SYNTHETIC_PIXEL','unit':'px'})
    read=LabelFile(str(path));assert read.shapes[0].label=='SYNTHETIC_AREA'
    assert [[float(p.x()),float(p.y())] for p in read.shapes[0].points]==shape['points']
    start=time.monotonic();model=SegmentAnything2ONNX(a.encoder,a.decoder,'cpu')
    embeddings=model.encode(image)
    masks=model.predict_masks(embeddings,[{'type':'point','data':[100,110],'label':1}])
    masks=np.asarray(masks);mask=masks.reshape(-1,256,256)[0]>0
    stats=save_mask(out,image,mask);assert stats['prompt_pixel_included']
    return {'status':'PASS','xany_version':app_info.__version__,'annotation_readback':'PASS','model_backend':'official X-AnyLabeling SegmentAnything2ONNX','onnxruntime':ort.__version__,'providers':model.encoder.session.get_providers(),'encoder_sha256':hashlib.sha256(pathlib.Path(a.encoder).read_bytes()).hexdigest(),'decoder_sha256':hashlib.sha256(pathlib.Path(a.decoder).read_bytes()).hexdigest(),'elapsed_s':time.monotonic()-start,**stats,'gui_observation':'SEPARATE_CLI_OPEN_AND_SCREENSHOT_REQUIRED'}

def critical(a,out):
    import numpy as np
    source=pathlib.Path(a.source)
    sys.path.insert(0,str(source/'uncharted-ta1'))
    from tasks.segmentation.entities import MapSegmentation,SegmentationResult
    from tasks.segmentation.segmenter_utils import rank_segments,get_segment_bounds
    from tasks.segmentation.denoise_segments import DenoiseSegments
    from tasks.common.task import TaskInput
    def segment(poly,score):
        return SegmentationResult(poly_bounds=poly,area=100,bbox=[0,0,10,10],class_label='legend_polygons',confidence=score,id_model='SYNTHETIC-NO-NEURAL-MODEL')
    spec=MapSegmentation(doc_id='SYNTHETIC',segments=[segment([(0,0),(10,0),(10,10),(0,10),(0,0)],.95),segment([(5,0),(15,0),(15,10),(5,10),(5,0)],.90)])
    rank_segments(spec,['legend_polygons']);assert spec.segments[0].confidence==.95
    inp=TaskInput(0);inp.data={'segmentation':spec.model_dump()}
    output=DenoiseSegments('synthetic-denoise').run(inp)
    merged=MapSegmentation.model_validate(output.output['segmentation']);assert len(merged.segments)==1
    bounds=get_segment_bounds(merged,'legend_polygons');assert len(bounds)==1 and abs(bounds[0].area-150)<1e-9
    write(out/'uncharted_postprocess.json',merged.model_dump())
    module_path=source/'uiuc-pipeline/src/patching.py'
    modspec=importlib.util.spec_from_file_location('vkm_uiuc_patch',module_path);module=importlib.util.module_from_spec(modspec);modspec.loader.exec_module(module)
    patches=np.full((1,2,2,1,4,4),7,dtype=np.uint8)
    rebuilt=module.unpatch_img(patches,(1,8,8),overlap=0,mode='discard')
    assert rebuilt.shape==(1,8,8) and np.all(rebuilt==7)
    np.save(out/'uiuc_unpatch.npy',rebuilt)
    return {'status':'LOCAL_COMPONENTS_PASS_INFERENCE_BLOCKED','uncharted_denoise_rank_union':'PASS','uiuc_unpatch_reconstruction':'PASS','external_steps':'DISABLED: Google Vision/OpenAI/CDR/RabbitMQ/S3 credentials not used','uncharted_inference':'BLOCKED_OFFICIAL_WEIGHTS_NOT_LOCATED; config.yaml/config.json/model_final.pth required; Detectron2 legacy torch2.0/torchvision0.15 incompatible with existing sm120 runtime; no shared downgrade','uiuc_inference':'BLOCKED_OFFICIAL_CHECKPOINT_NOT_LOCATED; required golden_muscat-0.3.ckpt; official releases have no assets; submodule/model container route needs bounded follow-up'}

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['sam','xany','critical']);p.add_argument('--out',required=True);p.add_argument('--checkpoint');p.add_argument('--encoder');p.add_argument('--decoder');p.add_argument('--source');p.add_argument('--gpu-lock');a=p.parse_args()
    out=pathlib.Path(a.out);out.mkdir(parents=True,exist_ok=False)
    try:
        code_hash=hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
        result={'sam':sam,'xany':xany,'critical':critical}[a.mode](a,out)
        result['script_sha256']=code_hash
        result.update(python=sys.version,executable=sys.executable,finished_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
        result['artifacts']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()}
        write(out/'receipt.json',result);print(json.dumps(result,ensure_ascii=False))
        return 0 if result['status']=='PASS' else 2
    except Exception as exc:
        write(out/'receipt.json',{'status':'FAIL','error':type(exc).__name__+': '+str(exc)});raise

if __name__=='__main__':raise SystemExit(main())
