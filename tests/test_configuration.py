import tempfile,unittest,sys
from retro_specialists.plugin import PrimitiveTools
from tests.test_plugin import Context
class ConfigurationTests(unittest.TestCase):
 def test_literature_optimization_settings_reach_trusted_worker(self):
  options={'literature_include_fulltext':False,'literature_context_chars':1500,'literature_cache_seconds':0,'literature_fulltext_timeout_seconds':4}
  with tempfile.TemporaryDirectory() as folder:
   trusted=PrimitiveTools(Context(folder,backend_python=sys.executable,**options))._configuration()[-1]
   for key,value in options.items():self.assertEqual(trusted[key],value)
