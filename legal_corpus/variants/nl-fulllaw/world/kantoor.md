# Canon — Meridiaan Advocaten (algemene praktijk, Nederlandse variant)

*Vaste feiten voor de Nederlandse full-law corpusgenerator. Het kantoor en de advocaten zijn de
stabiele canon; cliënten, wederpartijen en dossiers worden per matter gegenereerd (zie
`gen_lawfox.py`, firm-config `fulllaw`). Alle gegenereerde cliëntgegevens zijn **fictief**. Citeer
wetsartikelen exact zoals hieronder.*

## Het kantoor
**Meridiaan Advocaten B.V.**, gevestigd in Eindhoven (Kennedyplein 200). Een breed
algemene-praktijkkantoor dat elk belangrijk rechtsgebied bestrijkt: arbeidsrecht, familierecht,
strafrecht, belastingrecht, huur/vastgoed, ondernemingsrecht, bestuursrecht, insolventierecht,
letselschade/aansprakelijkheid en migratierecht, naast een technologiegroep voor ICT, privacy/AVG,
intellectueel eigendom en internetrecht. Cliënten variëren van particulieren tot mkb en grotere
ondernemingen. Het werk valt onder Nederlands en EU-recht; stukken worden in het Nederlands opgesteld.

## Advocaten en praktijkgebieden
| Advocaat | Praktijkgebied |
|---|---|
| mr. T. Bergsma | ICT / technologiecontracten |
| mr. L. Okafor | privacy / AVG |
| mr. S. Reinders | intellectueel eigendom (IE) |
| mr. N. Vrugt | internetrecht, e-commerce, platformaansprakelijkheid |
| mr. J. Havelaar | arbeidsrecht |
| mr. C. de Wit | familierecht |
| mr. R. Bahadoer | strafrecht |
| mr. F. Lindeman | belastingrecht |
| mr. A. Koster | huur- / vastgoedrecht |
| mr. M. Terhorst | ondernemingsrecht |
| mr. P. Slootweg | bestuursrecht |
| mr. D. van Raay | insolventierecht |
| mr. E. Nolte | letselschade / aansprakelijkheid |
| mr. H. Yildiz | migratie- / asielrecht |

## Wetsartikelen per praktijkgebied (citeer exact)
**ICT / verbintenissenrecht** (Burgerlijk Wetboek = BW)
- art. 6:74 BW (tekortkoming/schade); art. 6:248 BW en 6:248 lid 2 BW (redelijkheid en billijkheid)
- art. 7:401 BW (zorgplicht); art. 6:265 BW (ontbinding); art. 6:233 BW (algemene voorwaarden)

**Privacy / AVG**
- art. 6 / 13 / 28 / 32 AVG; art. 33 AVG (melding aan de toezichthouder — 72 uur);
  art. 34 AVG (melding aan betrokkenen); art. 35 AVG (DPIA)

**Intellectueel eigendom**
- art. 1 / 13 / 27 Auteurswet; art. 2.20 BVIE (merk); art. 1019h Rv (volledige proceskosten);
  art. 7:900 BW (vaststellingsovereenkomst)

**Internetrecht / e-commerce**
- art. 6:196c BW (hosting-/tussenpersoonaansprakelijkheid); art. 3:15d BW (informatieplichten);
  Verordening (EU) 2022/2065 (Digital Services Act, DSA)

**Arbeidsrecht**
- art. 7:610 BW (arbeidsovereenkomst); art. 7:669 BW (ontslaggronden);
  art. 7:671b BW (ontbinding door de kantonrechter); art. 7:673 BW (transitievergoeding);
  art. 7:653 BW (concurrentiebeding)

**Familierecht**
- art. 1:157 BW (partneralimentatie); art. 1:404 BW (kinderalimentatie);
  art. 1:100 BW (verdeling gemeenschap); art. 1:253a BW (gezamenlijk gezag)

**Strafrecht**
- art. 310 Sr (diefstal); art. 300 Sr (mishandeling); art. 420bis Sr (witwassen);
  art. 6 EVRM (eerlijk proces); art. 67 Sv (voorlopige hechtenis)

**Belastingrecht**
- art. 16 AWR (navordering / nieuw feit); art. 67d AWR (vergrijpboete);
  art. 8 Wet Vpb 1969; art. 3.8 Wet IB 2001

**Huur / vastgoed**
- art. 7:290 BW (bedrijfsruimte); art. 7:274 BW (opzegging/ontruiming);
  art. 7:206 BW (gebreken/herstel); art. 7:230a BW (overige bedrijfsruimte)

**Ondernemingsrecht**
- art. 2:9 BW (behoorlijke taakvervulling bestuurder); art. 2:216 BW (uitkering/dividendtest);
  art. 2:239 BW (tegenstrijdig belang); art. 2:345 BW (enquêteprocedure)

**Bestuursrecht** (Algemene wet bestuursrecht = Awb)
- art. 6:7 Awb (bezwaartermijn); art. 8:1 Awb (beroep); art. 4:84 Awb (afwijking van beleid);
  art. 3:4 Awb (evenredigheid)

**Insolventierecht** (Faillissementswet = Fw)
- art. 1 Fw (faillissement); art. 42 Fw (pauliana/vernietiging); art. 53 Fw (verrekening);
  art. 40 Fw (opzegging overeenkomsten/arbeid)

**Letselschade / aansprakelijkheid**
- art. 6:162 BW (onrechtmatige daad); art. 6:98 BW (causaal verband); art. 6:106 BW (smartengeld);
  art. 6:107 BW (verplaatste schade)

**Migratie / asiel**
- art. 8 Vw 2000 (rechtmatig verblijf); art. 29 Vw 2000 (asiel); art. 8 EVRM (gezinsleven);
  art. 67 Vw 2000 (ongewenstverklaring)

## Drafting-regels
- Schrijf volledig in het **Nederlands**, in de toon en structuur van het documenttype.
- Gebruik de advocaat van het betreffende praktijkgebied als auteur van advies/correspondentie.
- `privileged: true` voor vertrouwelijk advies aan de cliënt en processtukken/work product;
  `privileged: false` voor ondertekende contracten en correspondentie met de wederpartij / instanties.
- Geen plaatshouders ([TBD], lorem ipsum, <...>). Houd bedragen, data en artikelen consistent binnen een dossier.
