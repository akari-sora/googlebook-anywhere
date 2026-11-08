import argparse,pathlib,re,subprocess,tempfile,unittest
parser=argparse.ArgumentParser()
parser.add_argument("--source",required=True,type=pathlib.Path)
args=parser.parse_args()
D=args.source
class Regression(unittest.TestCase):
 def test_simple_pipeline_defaults_from_source(self):
  text=(D/'src/libcamera/pipeline/simple/simple.cpp').read_text()
  start=text.index('switch (info.colourEncoding)',text.index('if (!cfg.colorSpace)'))
  end=text.index('\n\t\t\t/*',start)
  switch=text[start:end]
  code="""#include <optional>
#include <cassert>
struct ColorSpace { enum Value { Srgb, Sycc, Raw }; };
struct PixelFormatInfo { enum Encoding { ColourEncodingRGB, ColourEncodingYUV, ColourEncodingRAW }; Encoding colourEncoding; };
struct C { std::optional<ColorSpace::Value> colorSpace; };
int main() { PixelFormatInfo info{PixelFormatInfo::ColourEncodingYUV}; C cfg;
"""+switch+"\nassert(cfg.colorSpace == ColorSpace::Sycc); }\n"
  with tempfile.TemporaryDirectory() as t:
   src=pathlib.Path(t)/'simple.cpp';src.write_text(code);exe=pathlib.Path(t)/'simple'
   subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(src),'-o',str(exe)],check=True)
   subprocess.run([str(exe)],check=True)
 def test_real_format_change_snippets(self):
  s=(D/'src/android/camera_capabilities.cpp').read_text()
  sections=[s[s.index('CameraCapabilities::initializeYUVResolutions'):s.index('CameraCapabilities::initializeRawResolutions')],s[s.index('for (const PixelFormat &pixelFormat : libcameraFormats)'):s.index('if (!mappedFormat.isValid())')]]
  snippets=[]
  for section in sections:
   m=re.search(r'if \(cfg\.pixelFormat != pixelFormat\)\s*cfg\.colorSpace\.reset\(\);\s*cfg\.pixelFormat = pixelFormat;',section)
   self.assertIsNotNone(m);snippets.append(m.group())
  code='''#include <optional>\n#include <cassert>\nstruct C { int pixelFormat; std::optional<int> colorSpace; };\n'''
  for i,snip in enumerate(snippets): code+=f'void apply{i}(C &cfg, int pixelFormat) {{ {snip} }}\n'
  code+='int main() {\n'
  for i in range(len(snippets)):
   code+=f'C c{i}{{1,42}}; apply{i}(c{i},2); assert(c{i}.pixelFormat==2 && !c{i}.colorSpace); c{i}.colorSpace=7; apply{i}(c{i},2); assert(c{i}.colorSpace==7);\n'
  code+='}\n'
  with tempfile.TemporaryDirectory() as t:
   src=pathlib.Path(t)/'test.cpp';src.write_text(code);exe=pathlib.Path(t)/'test'
   subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(src),'-o',str(exe)],check=True)
   subprocess.run([str(exe)],check=True)
if __name__=='__main__':unittest.main(argv=[__file__])
