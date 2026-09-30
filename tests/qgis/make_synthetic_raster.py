"""Executed only by the existing QGIS Python to create a small synthetic test raster."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from vkm_qgis.worker import bootstrap,LOCAL_WKT
from vkm_qgis.policy import Paths
paths=Paths.from_env(); app=bootstrap(paths)
from osgeo import gdal
import numpy as np
target=paths.new_output(sys.argv[1],suffixes={'.tif'})
ds=gdal.GetDriverByName('GTiff').Create(str(target),32,24,1,gdal.GDT_UInt16)
ds.SetGeoTransform((12,.5,0,20,0,-.5)); ds.SetProjection(LOCAL_WKT)
ds.GetRasterBand(1).WriteArray(np.arange(768,dtype=np.uint16).reshape(24,32)); ds.FlushCache(); ds=None
app.exitQgis()
print('SYNTHETIC_RASTER_READY')
