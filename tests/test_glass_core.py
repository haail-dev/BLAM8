"""Production read-only meter adapter replay, no capture/provider."""
import subprocess
from pathlib import Path

def test_owned_voice_meter_replay():
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(["node","tests/replays/voice_meter_replay.mjs"],cwd=root,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_generated_mechanical_masks_preserve_user_material_identity():
    import base64,hashlib,re
    root=Path(__file__).resolve().parents[1]
    source=(root/'src/app/static/blam8-particle-map.js').read_text(encoding='utf-8')
    points=base64.b64decode(re.search(r'BLAM8_PARTICLE_MAP = "([^"]+)"',source)[1])
    mask=base64.b64decode(re.search(r'BLAM8_GEAR_MASK = "([^"]+)"',source)[1])
    assert len(points)==3000*4 and len(mask)==128*128//2
    assert hashlib.sha256(points[3::4]).hexdigest()=="6de6c27ebc2a360769d0bc4eb8038a52e724ea31c4114006eda4250ee1d17285"
    result=subprocess.run(['python','scripts/build/derive_blam8_particle_map.py','--check'],cwd=root,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
