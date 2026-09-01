// ROOT smoke test.  Explicit #includes on purpose: they exercise the header
// search path and cling's precompiled header, both of which live at absolute
// paths under the install prefix.
#include "TFile.h"
#include "TH1F.h"
#include "TRandom3.h"
#include <cstdio>

void smoke()
{
    const char *fname = "/tmp/multispack_smoke.root";
    TRandom3 rng(12345);
    {
        TFile f(fname, "RECREATE");
        TH1F h("h", "multispack", 20, 0., 1.);
        for (int i = 0; i < 1000; ++i) h.Fill(rng.Uniform());
        h.Write();
        f.Close();
    }
    {
        TFile f(fname, "READ");
        TH1F *h = (TH1F *)f.Get("h");
        if (!h) { printf("SMOKE FAIL no histogram\n"); return; }
        printf("SMOKE entries=%d mean=%.4f\n", (int)h->GetEntries(), h->GetMean());
        f.Close();
    }
}
