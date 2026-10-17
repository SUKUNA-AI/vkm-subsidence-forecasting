#!/usr/bin/env python3
"""Labelled DSM engineering views and unchanged pixel-centre Civil 3D inputs; no CAD process is started."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np


def raster_mesh(z, gt, nodata=None):
    if gt[2] or gt[4] or gt[1] <= 0 or gt[5] >= 0:
        raise ValueError("Expected a north-up metre grid")
    valid=np.isfinite(z) & (True if nodata is None else z!=nodata)
    row,col=np.nonzero(valid)
    xyz=np.column_stack((gt[0]+(col+.5)*gt[1],gt[3]+(row+.5)*gt[5],z[valid]))
    index=np.full(z.shape,-1,dtype="int64");index[valid]=np.arange(len(row))
    r,c=np.nonzero(valid[:-1,:-1]&valid[1:,:-1]&valid[:-1,1:]&valid[1:,1:])
    triangles=np.concatenate((np.column_stack((index[r,c],index[r+1,c],index[r,c+1])),
                              np.column_stack((index[r,c+1],index[r+1,c],index[r+1,c+1]))))
    return xyz,triangles,row,col


def write_landxml(path, xyz, triangles):
    ns="http://www.landxml.org/schema/LandXML-1.2"
    ET.register_namespace("",ns)
    root=ET.Element("{"+ns+"}LandXML",{"version":"1.2","language":"English","readOnly":"false"})
    units=ET.SubElement(root,"{"+ns+"}Units")
    ET.SubElement(units,"{"+ns+"}Metric",{"areaUnit":"squareMeter","linearUnit":"meter","volumeUnit":"cubicMeter",
                  "temperatureUnit":"celsius","pressureUnit":"milliBars","angularUnit":"decimal degrees","directionUnit":"decimal degrees"})
    surfaces=ET.SubElement(root,"{"+ns+"}Surfaces")
    surface=ET.SubElement(surfaces,"{"+ns+"}Surface",{"name":"SKRU1_COPDEM_DSM_EGM2008_NOT_DTM"})
    definition=ET.SubElement(surface,"{"+ns+"}Definition",{"surfType":"TIN"})
    points=ET.SubElement(definition,"{"+ns+"}Pnts")
    for i,(x,y,z) in enumerate(xyz,1):
        ET.SubElement(points,"{"+ns+"}P",{"id":str(i)}).text=f"{y:.6f} {x:.6f} {z:.9f}"  # LandXML N E Z.
    faces=ET.SubElement(definition,"{"+ns+"}Faces")
    for tri in triangles:
        ET.SubElement(faces,"{"+ns+"}F").text=" ".join(str(int(i)+1) for i in tri)
    ET.ElementTree(root).write(path,encoding="utf-8",xml_declaration=True)
    reread=ET.parse(path)
    p=reread.findall(".//{"+ns+"}P");f=reread.findall(".//{"+ns+"}F")
    assert len(p)==len(xyz) and len(f)==len(triangles)
    decoded=np.array([[float(v) for v in node.text.split()] for node in p])[:,[1,0,2]]
    assert np.allclose(decoded,xyz,atol=1e-8,rtol=0),"LandXML N/E axis or height mismatch"
    return {"xml_read_back":"PASS","points":len(p),"faces":len(f),"native_import":"NOT_RUN"}


def read_plan_outlines(package):
    from osgeo import ogr
    path=package/"mining/mining.gpkg"
    dataset=ogr.Open(str(path));layer=dataset.GetLayerByName("skru1_boundaries_utm_scenario")
    def rings(geometry):
        if geometry.GetGeometryName() in {"LINESTRING","LINEARRING"}:
            yield np.array(geometry.GetPoints())[:,:2]
        else:
            for index in range(geometry.GetGeometryCount()):
                yield from rings(geometry.GetGeometryRef(index))
    outlines=[]
    if layer:
        for feature in layer:
            geometry=feature.GetGeometryRef()
            if geometry and not geometry.IsEmpty():
                outlines.extend(rings(geometry))
    dataset=None
    return outlines


def plot_views(z, gt, out, outlines=()):
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import LinearLocator, ScalarFormatter
    from matplotlib.lines import Line2D
    defaults={k:v for k,v in matplotlib.rcParamsDefault.items() if k!="backend"}
    with matplotlib.rc_context(rc={**defaults,"font.family":["DejaVu Sans"],"font.size":9}):
        rows,cols=z.shape
        x=gt[0]+(np.arange(cols)+.5)*gt[1]; y=gt[3]+(np.arange(rows)+.5)*gt[5]
        xx,yy=np.meshgrid(x,y)
        for exaggeration in (1,10):
            fig=Figure(figsize=(15,9),dpi=180);FigureCanvasAgg(fig)
            ax=fig.add_axes([.02,.17,.58,.68],projection="3d")
            surf=ax.plot_surface(xx,yy,z,cmap="terrain",rstride=3,cstride=3,linewidth=0,antialiased=False,
                                 vmin=float(np.nanmin(z)),vmax=float(np.nanmax(z)))
            ax.set_box_aspect((float(np.ptp(x)),float(np.ptp(y)),float(np.nanmax(z)-np.nanmin(z))*exaggeration))
            ax.set_xlim(float(x.min()),float(x.max()));ax.set_ylim(float(y.min()),float(y.max()))
            ax.set_zlim(float(np.nanmin(z)),float(np.nanmax(z)))
            ax.view_init(elev=32,azim=-55)
            ax.set_xlabel("X — восток, м (UTM 40N)",labelpad=9)
            ax.set_ylabel("Y — север, м (UTM 40N)",labelpad=12)
            ax.set_zlabel("Z, м EGM2008",labelpad=7)
            for axis in (ax.xaxis,ax.yaxis):
                axis.set_major_locator(LinearLocator(3));formatter=ScalarFormatter(useOffset=False)
                formatter.set_scientific(False);axis.set_major_formatter(formatter)
            # Avoid two seven/six-digit coordinate labels at the same front-right 3D corner.
            ax.set_xticks([float(x.min()),float((x.min()+x.max())/2)])
            ax.zaxis.set_major_locator(LinearLocator(2));ax.tick_params(labelsize=7,pad=1)
            if exaggeration==1:
                ax.set_zticks([])  # At true scale the 134 m Z axis is too short for two readable tick labels.
                ax.text2D(.03,.88,f"Z = {np.nanmin(z):.2f}…{np.nanmax(z):.2f} м EGM2008\n×1: 1 м по Z = 1 м по X/Y",
                          transform=ax.transAxes,fontsize=9)
            ax.set_title(f"Объёмный вид · вертикальный масштаб ×{exaggeration}",pad=12)
            plan=fig.add_axes([.67,.21,.27,.60])
            image=plan.imshow(z,extent=[x[0]-15,x[-1]+15,y[-1]-15,y[0]+15],origin="upper",cmap="terrain",
                              vmin=float(np.nanmin(z)),vmax=float(np.nanmax(z)))
            plan.set_title("План поверхности · север ↑")
            for outline in outlines:
                plan.plot(outline[:,0],outline[:,1],color="black",lw=.9)
            if len(outlines):
                plan.legend([Line2D([0],[0],color="black",lw=1)],
                            ["СКРУ-1: контуры плана\n(сценарная XY-привязка)"],loc="lower right",fontsize=7,
                            framealpha=.9)
            plan.set_xlabel("X — восток, м (UTM 40N)");plan.set_ylabel("Y — север, м")
            plan.ticklabel_format(style="plain",useOffset=False);plan.tick_params(labelsize=7)
            plan.annotate("С",xy=(.91,.96),xytext=(.91,.78),xycoords="axes fraction",ha="center",
                          arrowprops={"arrowstyle":"->","lw":2},fontsize=12)
            bx=x.min()+.08*np.ptp(x);by=y.min()+.08*np.ptp(y)
            plan.plot([bx,bx+2000],[by,by],color="black",lw=4)
            plan.text(bx+1000,by+200,"2000 м",ha="center",bbox={"facecolor":"white","alpha":.85,"edgecolor":"none"})
            cb=fig.colorbar(image,ax=plan,fraction=.045,pad=.04);cb.set_label("Высота DSM, м · EGM2008")
            fig.suptitle("СКРУ-1 · цифровая поверхность Copernicus GLO-30 (DSM)",fontsize=17,y=.97)
            fig.text(.5,.89,"Съёмка TanDEM-X 2011–2015 · сетка 30 м · EPSG:32640 + EGM2008 height (3855)",ha="center",fontsize=11)
            fig.text(.05,.095,"DSM включает растительность и постройки. Это не DTM и не установленная геологическая граница.\n"
                     "Привязка к Балтийской системе и высотам выработок UNKNOWN. HEM — только случайная σ источника.\n"
                     "На 3D-превью показан каждый третий узел (90 м); CSV и LandXML сохраняют все исходные узлы 30 м.",fontsize=9)
            fig.savefig(out/f"skru1_dsm_x{exaggeration}.png",metadata={"Software":"VKM engineering DSM view"});fig.clear()


def build(package:Path,out:Path):
    from osgeo import gdal
    gdal.UseExceptions()
    package,out=package.resolve(),out.resolve()
    source=package/"terrain/data/dem_glo30_aoi_utm40n_30m.tif"
    ds=gdal.Open(str(source));gt=ds.GetGeoTransform();z=ds.ReadAsArray();nodata=ds.GetRasterBand(1).GetNoDataValue()
    xyz,triangles,row,col=raster_mesh(z,gt,nodata)
    if ds.GetSpatialRef().GetAuthorityCode(None) not in {"32640","9518"} and "32640" not in ds.GetProjection():
        raise ValueError("Expected explicit UTM40N projection")
    ds=None
    out.mkdir(parents=True,exist_ok=False)
    with (out/"skru1_dsm_points.csv").open("w",encoding="utf-8",newline="") as stream:
        writer=csv.writer(stream,lineterminator="\n");writer.writerow(["name","x","y","z","desc","source_row","source_col"])
        for (x,y,h),r,c in zip(xyz,row,col):
            writer.writerow([f"COPDEM_r{r:04d}_c{c:04d}",f"{x:.6f}",f"{y:.6f}",f"{h:.9f}","DSM_EGM2008_NOT_DTM",int(r),int(c)])
    landxml=write_landxml(out/"skru1_dsm_grid_tin.xml",xyz,triangles)
    shown=z.astype("float64");shown[~np.isfinite(shown)|(shown==nodata)]=np.nan
    plot_views(shown,gt,out,read_plan_outlines(package))
    command={"steps":[{"tool":"cad_job_create","args":{"product":"C3D","label":"SKRU1 DSM Copernicus30m EGM2008 ×1 (not DTM)"}},
        {"tool":"c3d_points_from_table","args":{"job_id":"<returned_job_id>","table":"<absolute out>/skru1_dsm_points.csv",
         "columns":{"name":"name","x":"x","y":"y","z":"z","desc":"desc"},"point_group":"SKRU1_DSM30M",
         "units":"m","delimiter":",","decimal":".","engine":"civil3d","name_policy":"as_is",
         "georeference":{"type":"offset","params":{"dx":0,"dy":0},"basis":"Input DSM is already UTM40N EPSG32640; identity XY; Z unchanged EGM2008, Baltic relation UNKNOWN","target_crs_label":"EPSG:32640 XY; Z EGM2008 (EPSG3855)"}}},
        {"tool":"c3d_tin_surface","args":{"job_id":"<returned_job_id>","name":"SKRU1_DSM_EGM2008_NOT_DTM",
         "point_group":"SKRU1_DSM30M","engine":"civil3d","units":"m"}}],
         "native_run":"NOT_RUN; ROOT is sole CAD job writer","note":"Civil 3D Delaunay may choose different equal-length diagonals than explicit LandXML grid; no new accuracy; no edge cutoff needed for this complete rectangular grid (0 missing pixels)"}
    (out/"civil3d_mcp_commands.json").write_text(json.dumps(command,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    receipt={"schema":"vkm.skru1_dsm_engineering/1","source":source.relative_to(package).as_posix(),"source_sha256":sha(source),
       "code_sha256":sha(Path(__file__)),"planimetry_overlay":"SKRU1 source plan boundaries, scenario XY registration; no physical underground Z",
       "grid":{"rows":z.shape[0],"cols":z.shape[1],"pixel_m":gt[1],"valid_points":len(xyz),"nodata":int(z.size-len(xyz))},
       "elevation_EGM2008_m":[float(xyz[:,2].min()),float(xyz[:,2].max())],"landxml":landxml,
       "surface_kind":"DSM_NOT_DTM","epistemic_status":"DERIVATION; TIN between centres INTERPOLATION/MODEL_CHOICE",
       "geological_upper_boundary":"UNKNOWN; DSM can be a conditional world boundary only after explicit MODEL_CHOICE, not bare-earth geological contact",
       "epoch":"TanDEM-X acquisition2011–2015; no present-day observation claim","native_civil3d":"NOT_RUN",
       "outputs_sha256":{p.name:sha(p) for p in sorted(out.iterdir()) if p.is_file()}}
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return receipt


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--package",type=Path,required=True);parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args();print(json.dumps(build(args.package,args.out),ensure_ascii=False,indent=2))
