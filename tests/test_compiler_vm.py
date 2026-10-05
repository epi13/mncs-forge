"""Real retained Forge kernel on explicitly selected compiler/VM providers."""
import json
import tempfile
import unittest
from pathlib import Path
from mncs_forge.compiler_vm import CanonicalVmApplication

ROOT=Path(__file__).resolve().parents[1];WS=ROOT.parent
COMPILER=WS/'mncs-compiler';EXE=COMPILER/'.bootstrap/target/release/mncs-compiler-stage0-probe';VM=WS/'mncs-vm/target/debug/mncs-vm'

@unittest.skipUnless(EXE.is_file() and VM.is_file(),'selected compiler/VM not built')
class RetainedCanonicalTests(unittest.TestCase):
    def test_real_kernel_oracle_agreement_and_one_process(self):
        source=ROOT/'src/mncs_forge/resources/native/forge/core.mncs'
        libraries=[ROOT/'src/mncs_forge/resources/native',WS/'mncs-stdlib/library']
        request={'schema_version':'0.1','target':{'module':'mncs.forge.core.v1','function':'lifecycle_initial'},'arguments':[],'step_budget':100000}
        with tempfile.TemporaryDirectory() as raw:
            app=CanonicalVmApplication(compiler_checkout=COMPILER,vm_checkout=WS/'mncs-vm',executable=VM,cache=Path(raw),compiler_executable=EXE)
            try:
                first=app.call(source,request,libraries=libraries);pid=app.session.proc.pid
                second=app.call(source,request,libraries=libraries)
                self.assertEqual(app.session.proc.pid,pid);self.assertTrue(app.status()['cache_reused'])
                self.assertEqual(first['execution']['returned'],second['execution']['returned'])
                self.assertEqual(first['record']['usage'],second['record']['usage'])
                oracle=app.producer.emit({'schema_version':'mncs.compiler-vm-request/1','source':str(source),'logical_name':source.name,'libraries':[str(p) for p in libraries],'calls':[request]},Path(raw))
                evidence=json.loads((Path(raw)/oracle['evidence']['address']).read_text())
                self.assertEqual(first['execution']['returned'],evidence['reference_results'][0]['returned'])
                self.assertEqual(first['outcome']['kind'],'completed')
                with self.assertRaisesRegex(RuntimeError,'host capability'):app.call(source,{**request,'grants':['process']},libraries=libraries)
            finally:app.close()
