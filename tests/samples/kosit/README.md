# Official XRechnung test invoices (KoSIT)

These files are unchanged copies from the official XRechnung test suite published by KoSIT
(Koordinierungsstelle für IT-Standards):

- Source: https://github.com/itplr-kosit/xrechnung-testsuite, folder `src/test/business-cases/standard`
- Version: commit `1ce1dafffd035e5ce59a7ab0d3bf44ac14af16c9` (4 Sep 2026), XRechnung 3.0
- Licence: Apache License 2.0, see [LICENSE](LICENSE) in this folder

Each business case comes in both XML syntaxes of the standard: `*_ubl.xml` (OASIS UBL 2.1) and
`*_uncefact.xml` (UN/CEFACT CII). The tests parse every file and check that both versions of a
case give the same invoice and that the totals add up. The selection keeps the repository small
(pairs under 60 KB), plus case 03.05a, whose unit price with 9 decimals led to migration 0002.

Three pairs differ in the source files themselves, so the tests compare their supplier data
separately: 01.20a (placeholder texts in the address), 01.21a (different seller VAT ID) and
03.06a (postcode and city swapped in the UBL file).
