"""Synthetic regression measurements. No real DPM or optical validation claims."""
import json
from pathlib import Path
import sys
import time
import cv2
import numpy as np
import psutil
import zxingcpp
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from backend.config import Settings
from backend.vision import Pipeline

cfg=Settings(barcode_mode="qr_and_data_matrix",confirmation_frames=2,decoder_budget_ms=120)
trials=[]
for sym in (zxingcpp.BarcodeFormat.QRCode,zxingcpp.BarcodeFormat.DataMatrix):
    raw=np.asarray(zxingcpp.create_barcode('LAB-SYNTHETIC-001',sym).to_image(scale=7,add_quiet_zones=True))
    for rotation in range(4):
        image=cv2.cvtColor(np.ascontiguousarray(np.rot90(raw,rotation)),cv2.COLOR_GRAY2BGR)
        for condition in ('clean','low_contrast','blur','inverted'):
            treated=image.copy()
            if condition=='low_contrast':treated=(treated*.25+95).astype(np.uint8)
            if condition=='blur':treated=cv2.GaussianBlur(treated,(7,7),1.5)
            if condition=='inverted':treated=255-treated
            p=Pipeline()
            timings=[]
            accepted=[]
            start=time.perf_counter()
            for frame in range(4):
                canvas=np.full((720,1280,3),180,np.uint8)
                y=100+frame*9;x=750+frame*12
                canvas[y:y+treated.shape[0],x:x+treated.shape[1]]=treated
                result=p.process(canvas,cfg)
                timings.append(result['processing_ms'])
                accepted.extend(r['value'] for r in result['confirmed'])
            trials.append({'symbology':str(sym),'rotation_degrees':rotation*90,'condition':condition,
                           'correct': 'LAB-SYNTHETIC-001' in accepted,'false_values':[v for v in accepted if v!='LAB-SYNTHETIC-001'],
                           'processing_ms':timings,'run_elapsed_ms':round((time.perf_counter()-start)*1000,2)})
report={'label':'SYNTHETIC REGRESSION ONLY — not engraved-metal performance',
        'python':sys.version,'opencv':cv2.__version__,'settings':cfg.model_dump(),'trials':trials,
        'samples':len(trials),'correct':sum(t['correct'] for t in trials),'false_confirmations':sum(len(t['false_values']) for t in trials),
        'mean_frame_ms':float(np.mean([v for t in trials for v in t['processing_ms']])),
        'p95_frame_ms':float(np.percentile([v for t in trials for v in t['processing_ms']],95)),
        'rss_mb':round(psutil.Process().memory_info().rss/1048576,1)}
output=Path(__file__).resolve().parent.parent/'docs'/'synthetic-benchmark.json'
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k in ('samples','correct','false_confirmations','mean_frame_ms','p95_frame_ms','rss_mb')}))
