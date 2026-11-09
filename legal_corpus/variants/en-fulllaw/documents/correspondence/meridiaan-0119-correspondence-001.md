---
doc_id: meridiaan-0119-correspondence-001
matter: 2025.0119
doc_type: correspondence
title: Notice of Personal Data Breach – Unauthorized Access to Backup Systems
date: 2025-04-12
author: Meridiaan Media B.V.
recipient: Veldhoven Interactive B.V.
privileged: false
parties: [Meridiaan Media B.V., Veldhoven Interactive B.V.]
statutes_cited: [Article 33 GDPR]
summary: Formal notification of a personal data breach involving unauthorized access to backup systems, reported within the mandated timeframe with details on nature, scope, and remedial measures.
---
**Meridiaan Media B.V.**
Keizer Karelstraat 15
5611 GL Eindhoven
The Netherlands
T: +31 (0)40 123 4567
E: compliance@meridianamedia.nl

**Veldhoven Interactive B.V.**
Attn: Data Protection Officer / Legal Department
Industrieweg 88
5508 LH Veldhoven
The Netherlands

**Via Email:** dpo@veldhoveninteractive.nl
**Date:** 12 April 2025

**Subject: Notice of Personal Data Breach pursuant to Article 33 GDPR – Incident Ref. 2025-INC-042**

Dear Sir or Madam,

We are writing to formally notify you of a personal data breach concerning the processing of personal data for which we, Meridiaan Media B.V., act as the Data Controller, and for which you, Veldhoven Interactive B.V., act as the Data Processor. This notification is provided in accordance with your obligations under **Article 33 GDPR** to report such breaches without undue delay and, where feasible, not later than 72 hours after having become aware of it.

We confirm that this breach was detected and reported to our internal management within **36 hours** of the initial occurrence, allowing for this timely notification to you as the processor responsible for the affected systems.

### 1. Nature of the Breach

On 10 April 2025 at approximately 14:30 CET, our security monitoring systems detected anomalous outbound traffic from a secondary cloud backup server maintained by Veldhoven Interactive B.V. Further investigation revealed that an unauthorized third party had gained access to this specific backup repository. The attacker exploited a misconfiguration in the authentication token validity period of an outdated API key associated with the backup service.

The breach involves the exfiltration of encrypted backup files containing personal data. While the data at rest was encrypted using AES-256, we have determined that the decryption keys were stored separately and remain secure; however, the integrity and confidentiality of the data cannot be guaranteed until further forensic analysis confirms that no keys were compromised alongside the storage credentials.

### 2. Scope of the Breach

The affected backup covers client records processed on behalf of Meridiaan Media B.V. between 1 January 2024 and 9 April 2025. The categories of personal data potentially affected include:

*   Full names of subscribers and prospects;
*   Email addresses linked to account registrations;
*   IP addresses and device identifiers logged during service usage;
*   Billing information (partially masked credit card numbers and billing addresses);
*   Support ticket histories containing user-generated content.

We estimate that approximately 14,500 data records were stored within the compromised backup volume. No special categories of personal data as defined in Article 9 GDPR (such as health or biometric data) are present in this specific dataset.

### 3. Measures Taken and Risk Mitigation

Upon detection of the anomaly, Veldhoven Interactive B.V. immediately isolated the affected server segment to prevent further unauthorized access. The following remedial actions have been initiated:

1.  **Containment:** Access credentials for the backup service were revoked globally, and all active sessions were terminated.
2.  **Forensic Analysis:** A third-party cybersecurity firm has been engaged to perform a root cause analysis. Preliminary findings confirm that the intrusion was limited to the specific backup node and did not impact primary production databases or customer-facing applications.
3.  **Encryption Review:** We are currently verifying the separation of encryption keys from the storage credentials to ensure they were not exposed during the incident.
4.  **Preventive Measures:** A comprehensive audit of all API keys and access tokens is underway to identify and rectify similar misconfigurations across our entire infrastructure. Multi-factor authentication has been enforced for all administrative access points effective immediately.

### 4. Assessment of Risk

Based on the current evidence, we assess the risk to the rights and freedoms of natural persons as **low**. The data was encrypted at rest, and there is no indication that decryption keys or sufficient personal details (such as full financial credentials) were exfiltrated in a usable format. However, due to the potential for future misuse if keys are later compromised, we request your continued cooperation in providing the final forensic report.

### 5. Next Steps

As the Data Controller, we will assess whether this breach requires notification to the Dutch Data Protection Authority (Autoriteit Persoonsgegevens) and/or the affected data subjects under Article 34 GDPR. We reserve the right to update this correspondence should new information become available that alters our risk assessment.

We request that you provide a written confirmation of receipt of this notice and share the preliminary forensic report once it is available, but no later than 20 April 2025.

Should you have any questions or require further clarification regarding this incident, please contact our Legal Department directly at legal@meridianamedia.nl.

Yours sincerely,

*(Signed)*

**mr. J. Havelaar**
Partner
Meridiaan Media B.V.