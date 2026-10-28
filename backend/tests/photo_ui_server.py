import sys,io
from pathlib import Path
from datetime import datetime, timezone, timedelta
root=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(root/'backend/.venv/Lib/site-packages'),str(root/'backend')]
from unittest.mock import Mock
from PIL import Image,ImageDraw
from app import app,latest
import queries,db
state=root/'.build/camera-ui/preview-photos.sqlite3'
state.parent.mkdir(parents=True,exist_ok=True)
app.config.update(PHOTO_DB_PATH=str(state),TESTING=True)
db.get_pool=lambda: Mock(connection=lambda:Mock())
now=datetime.now(timezone(timedelta(hours=8)))
events=[dict(id=1,device_id='esp32-preview',boot_id='preview1',seq=6,etype='TILT',up_ms=150000,created_at=now.strftime('%Y-%m-%d %H:%M:%S')),dict(id=2,device_id='esp32-preview',boot_id='preview1',seq=7,etype='FALL',up_ms=300000,created_at=now.strftime('%Y-%m-%d %H:%M:%S'))]
queries.recent_events=lambda *a,**k:events
queries.event_count=lambda *a,**k:2
queries.stats_summary=lambda *a,**k:dict(today=2,total=2,last=events[-1]['created_at'])
queries.page_events=lambda *a,**k:dict(items=events,total=2,page=1,per_page=50)
queries.samples_range=lambda *a,**k:[]
latest.update(online=True,device_id='esp32-preview',boot_id='preview1',up=610000,m=1.02,ang=3.5,air=15.0,state='ARMED',alarm=0,seq=7,last_seen=now.timestamp())
with app.test_client() as client:
 for seq in range(1,16):
  reason=('TIMER','FALL','AIR','MANUAL')[(seq-1)%4]
  im=Image.new('RGB',(800,600),'#cbd7de');draw=ImageDraw.Draw(im)
  draw.rectangle((20,20,780,580),outline='#536780',width=2)
  draw.text((80,240),'CAMERA UI TEST FIXTURE',fill='#142a47',font_size=36)
  draw.text((80,300),f'{reason}  /  Photo {seq:02d}  /  Not a hardware photo',fill='#23344d',font_size=22)
  out=io.BytesIO();im.save(out,format='JPEG')
  h={'Content-Type':'image/jpeg','X-Device-Id':'esp32-preview','X-Boot-Id':'preview1','X-Photo-Seq':str(seq),'X-Photo-Reason':reason,'X-Event-Seq':'7' if reason in {'FALL','AIR'} else '0','X-Trigger-Up-Ms':str(seq*300000),'X-Capture-Up-Ms':str(seq*300000+150)}
  r=client.post('/api/photos',data=out.getvalue(),headers=h)
  assert r.status_code in (200,201),(r.status_code,r.json)
print('Preview server: isolated fixture SQLite, mocked MySQL, port5069',flush=True)
app.run('127.0.0.1',5069,use_reloader=False,threaded=True)
