# Canon — Meridiaan Advocaten (general-practice, English variant)

*Fixed facts for the English general full-law corpus generator. The firm and the lawyers are the
stable canon; clients, counterparties and matters are generated per matter (see `gen_lawfox.py`,
firm config `fulllaw`). All generated client data is **fictional**. Cite statutes exactly as written
below.*

## The firm
**Meridiaan Advocaten B.V.**, based in Eindhoven (Kennedyplein 200). A broad general-practice law
firm covering every major area of Dutch law: employment, family, criminal, tax, real estate,
corporate, administrative, insolvency, personal injury/tort and migration, alongside a technology
group for ICT, privacy/GDPR, intellectual property and internet law. Clients range from private
individuals to SMEs and larger companies. Work is governed by Dutch and EU law; documents are drafted
in English for international clients.

## Lawyers and practice areas
| Lawyer | Practice area |
|---|---|
| mr. T. Bergsma | ICT / technology contracts |
| mr. L. Okafor | privacy / GDPR |
| mr. S. Reinders | intellectual property (IP) |
| mr. N. Vrugt | internet law, e-commerce, platform liability |
| mr. J. Havelaar | employment law |
| mr. C. de Wit | family law |
| mr. R. Bahadoer | criminal law |
| mr. F. Lindeman | tax law |
| mr. A. Koster | real estate / tenancy law |
| mr. M. Terhorst | corporate / company law |
| mr. P. Slootweg | administrative law |
| mr. D. van Raay | insolvency law |
| mr. E. Nolte | personal injury / tort |
| mr. H. Yildiz | migration / asylum law |

## Statutes per practice area (cite exactly)
**ICT / contract law** (Dutch Civil Code = DCC)
- Article 6:74 DCC (breach / damages); Article 6:248 DCC and 6:248(2) DCC (reasonableness and fairness)
- Article 7:401 DCC (duty of care); Article 6:265 DCC (rescission); Article 6:233 DCC (general terms)

**Privacy / GDPR**
- Article 6 / 13 / 28 / 32 GDPR; Article 33 GDPR (breach notification to the authority — 72 hours);
  Article 34 GDPR (notify data subjects); Article 35 GDPR (DPIA)

**Intellectual property**
- Article 1 / 13 / 27 Dutch Copyright Act; Article 2.20 BCIP (trademark); Article 1019h DCCP (full costs);
  Article 7:900 DCC (settlement)

**Internet law / e-commerce**
- Article 6:196c DCC (hosting/intermediary liability); Article 3:15d DCC (information duties);
  Regulation (EU) 2022/2065 (Digital Services Act, DSA)

**Employment law**
- Article 7:610 DCC (employment contract); Article 7:669 DCC (grounds for dismissal);
  Article 7:671b DCC (court dissolution); Article 7:673 DCC (transition payment);
  Article 7:653 DCC (non-compete clause)

**Family law**
- Article 1:157 DCC (spousal maintenance); Article 1:404 DCC (child maintenance);
  Article 1:100 DCC (division of the community); Article 1:253a DCC (joint parental authority)

**Criminal law**
- Article 310 Criminal Code (theft); Article 300 Criminal Code (assault);
  Article 420bis Criminal Code (money laundering); Article 6 ECHR (fair trial);
  Article 67 Code of Criminal Procedure (pre-trial detention)

**Tax law**
- Article 16 AWR (State Taxes Act — additional assessment / new fact); Article 67d AWR (penalty);
  Article 8 Corporate Income Tax Act 1969; Article 3.8 Income Tax Act 2001

**Real estate / tenancy**
- Article 7:290 DCC (business tenancy); Article 7:274 DCC (termination/eviction);
  Article 7:206 DCC (repairs/defects); Article 7:230a DCC (other business premises)

**Corporate / company law**
- Article 2:9 DCC (director's duty); Article 2:216 DCC (distribution/dividend test);
  Article 2:239 DCC (conflict of interest); Article 2:345 DCC (inquiry proceedings)

**Administrative law** (General Administrative Law Act = GALA / Awb)
- Article 6:7 GALA (objection period); Article 8:1 GALA (appeal); Article 4:84 GALA (policy departure);
  Article 3:4 GALA (proportionality)

**Insolvency law** (Bankruptcy Act)
- Article 1 Bankruptcy Act (bankruptcy); Article 42 Bankruptcy Act (fraudulent preference / avoidance);
  Article 53 Bankruptcy Act (set-off); Article 40 Bankruptcy Act (termination of contracts/employment)

**Personal injury / tort**
- Article 6:162 DCC (tort); Article 6:98 DCC (causation); Article 6:106 DCC (non-material damages);
  Article 6:107 DCC (third-party costs)

**Migration / asylum**
- Article 8 Aliens Act 2000 (lawful residence); Article 29 Aliens Act 2000 (asylum);
  Article 8 ECHR (family life); Article 67 Aliens Act 2000 (declaration undesirable)

## Drafting rules
- Write entirely in **English**, in the tone and structure of the document type.
- Use the lawyer matching the practice area as the author of advice/correspondence.
- `privileged: true` for confidential advice to the client and litigation work product;
  `privileged: false` for signed contracts and correspondence to the other side / to authorities.
- No placeholders ([TBD], lorem ipsum, <...>). Keep amounts, dates and articles consistent within a matter.
