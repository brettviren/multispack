"""PyROOT smoke test: exercises the Python bindings and cppyy's JIT."""
import sys
import ROOT

h = ROOT.TH1F("h", "multispack", 10, 0.0, 1.0)
for i in range(100):
    h.Fill(0.005 + i * 0.01)
ROOT.gInterpreter.Declare('int multispack_answer() { return 42; }')
print("PYROOT entries=%d answer=%d python=%d.%d" % (
    int(h.GetEntries()), ROOT.multispack_answer(),
    sys.version_info[0], sys.version_info[1]))
