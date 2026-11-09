---
doc_id: meridiaan-0105-correspondence-001
matter: 2025.0105
doc_type: correspondence
title: Notification of Personal Data Breach under Article 33 GDPR
date: 2025-04-12
author: Maaslicht Retail B.V.
recipient: Polderzicht Solutions B.V.
privileged: false
parties: [Maaslicht Retail B.V., Polderzicht Solutions B.V.]
statutes_cited: [Article 33 GDPR]
summary: Notification of unauthorized access to backup data and required remedial actions.
---
**From:** Maaslicht Retail B.V.
**To:** Polderzicht Solutions B.V.
**Date:** 12 April 2025
**Subject:** Notification of Personal Data Breach pursuant to Article 33 GDPR – Incident Ref. INC-2025-04-11

Dear Sir/Madam,

We are writing on behalf of Maaslicht Retail B.V. (hereinafter: "Maaslicht" or the "Controller") to formally notify Polderzicht Solutions B.V. (hereinafter: "Polderzicht" or the "Processor") of a personal data breach involving data processed by your organization under our Data Processing Agreement dated 15 January 2024.

**1. Nature of the Breach**

On the morning of 11 April 2025, Maaslicht’s IT security team detected anomalous activity in the cloud backup environment managed by Polderzicht for our customer loyalty database. Upon immediate investigation and forensic review, it was established that an unauthorized party gained access to a specific backup snapshot containing personal data of our customers.

The breach involves the following categories of data:
*   Customer names;
*   Email addresses;
*   Encrypted payment token identifiers (no raw credit card numbers were stored in this backup);
*   Purchase history logs from Q4 2024.

**2. Timeline and Reporting**

In accordance with our obligation to notify you of the breach without undue delay, and given that we reported the initial detection to your support desk at 14:00 CET on 11 April 2025, this formal notification is being issued within **36 hours** of the discovery. We confirm that this reporting period aligns with the requirement to notify the competent supervisory authority (the Autoriteit Persoonsgegevens) within 72 hours, should we deem it necessary after further assessment.

**3. Potential Consequences**

The unauthorized access poses a risk to the rights and freedoms of natural persons, specifically regarding privacy and potential identity theft or phishing attacks targeting the affected individuals. However, as the payment data was tokenized and other fields were partially masked in this specific backup instance, the immediate risk of financial fraud is considered low but non-zero due to the exposure of contact details for social engineering purposes.

**4. Measures Taken and Requested Actions**

Maaslicht has taken the following immediate measures:
*   Isolated the affected backup snapshot;
*   Forced a rotation of all API keys associated with the loyalty database interface;
*   Initiated a review of access logs to determine the extent of data exfiltration.

We hereby request that Polderzicht Solutions B.V.:
1.  Immediately secure the environment and confirm that the unauthorized access has been terminated;
2.  Provide a detailed technical report on the root cause of the breach within five (5) business days;
3.  Confirm which specific backup instances were affected and whether any data was exfiltrated outside your infrastructure;
4.  Cooperate fully with any subsequent investigation by the supervisory authority or law enforcement as required under Article 28 GDPR.

We reserve all rights to claim damages resulting from this breach pursuant to applicable law and our contractual agreements. Please acknowledge receipt of this notification immediately.

Yours sincerely,

mr. J. Havelaar
Legal Counsel for Maaslicht Retail B.V.
Meridiaan Advocaten B.V.
Kennedyplein 200, Eindhoven