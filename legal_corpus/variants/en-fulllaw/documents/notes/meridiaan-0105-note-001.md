---
doc_id: meridiaan-0105-note-001
matter: 2025.0105
doc_type: attendance_note
title: Attendance Note: Breach Notification Strategy for Maaslicht Retail B.V.
date: 2024-11-15
author: mr. L. Okafor
recipient: 
privileged: true
parties: [Maaslicht Retail B.V., Dutch DPA]
statutes_cited: [Article 33 GDPR]
summary: Attendance note regarding the urgent decision to notify the Dutch Data Protection Authority within the statutory period following a confirmed personal data breach involving customer payment details.
---
**MATTER:** 2025.0105
**CLIENT:** Maaslicht Retail B.V.
**DATE:** 15 November 2024
**ATTORNEY:** mr. L. Okafor (Privacy / GDPR)
**SUBJECT:** Immediate Breach Response: Notification to the Dutch Data Protection Authority

### 1. Context and Incident Overview

At 09:30 hours on 14 November 2024, Maaslicht Retail B.V.’s IT security team detected an unauthorized access event affecting their primary e-commerce database. The incident involves the potential exfiltration of personal data belonging to approximately 4,500 customers. The compromised data sets include names, email addresses, shipping addresses, and encrypted payment card details (though the encryption keys were not accessed).

Maaslicht Retail B.V. engaged our firm immediately upon confirmation of the breach scope. We verified that this constitutes a "personal data breach" under Article 4(12) GDPR. The primary legal obligation now rests on the client to notify the competent supervisory authority, specifically the Dutch Data Protection Authority (Autoriteit Persoonsgegevens).

### 2. Legal Analysis: Notification to the Supervisory Authority

Under **Article 33 GDPR**, the controller is obligated to notify a personal data breach to the supervisory authority without undue delay and, where feasible, not later than 72 hours after having become aware of it.

The clock started ticking at 09:30 on 14 November 2024. Therefore, the final deadline for notification is **72 hours** from that timestamp, which falls on 17 November 2024 at 09:30. Missing this window requires a justification for the delay, which creates unnecessary legal risk and potential administrative fines under Article 83(4)(a) GDPR.

Given that the breach involves financial data, even if encrypted, the risk to the rights and freedoms of natural persons is considered high due to the potential for identity theft and fraud. Consequently, notification is mandatory. We cannot rely on the exception where the breach is unlikely to result in a risk.

### 3. Decision: Notification Strategy

It has been decided that Maaslicht Retail B.V. will submit the notification to the Dutch DPA via their secure online reporting portal. The notification will include:
1. A description of the nature of the breach, including the categories and approximate number of data subjects concerned (4,500 individuals).
2. The name and contact details of the Data Protection Officer at Maaslicht Retail B.V. (ms. J. Visser, j.visser@maaslichtretail.nl).
3. A description of the likely consequences of the breach (potential financial fraud and identity theft).
4. A description of the measures taken or proposed to be taken to address the breach, including the deployment of a third-party forensic firm, DataGuard Forensics B.V., located in Amsterdam.

### 4. Assessment: Notification to Data Subjects (Article 34 GDPR)

We are currently assessing whether the breach requires communication to the affected data subjects under **Article 34 GDPR**. This article mandates notification to data subjects when the breach is likely to result in a high risk to their rights and freedoms.

**Factors favoring notification:**
*   The data includes shipping addresses, which could facilitate physical burglary or targeted phishing.
*   Payment card numbers were exposed, creating a direct financial risk.

**Factors mitigating the risk (reducing likelihood of "high risk"):**
*   The payment card data was encrypted using AES-256 standards.
*   The encryption keys remain secure and were not accessed by the attackers.
*   The email addresses are already publicly available in many contexts, reducing the incremental harm of their exposure.

**Preliminary Conclusion:**
While the encryption significantly mitigates the immediate financial risk, the combination of shipping addresses and partial payment data still poses a credible threat to personal security. It is recommended that we prepare draft communications for all 4,500 affected customers as a precautionary measure. However, final approval for sending these notifications will be deferred until the forensic report from DataGuard Forensics B.V. confirms the exact scope of usable data on **18 November 2024**. If the forensic report confirms that no unencrypted sensitive financial data was exfiltrated, we may argue to the DPA that direct notification to individuals is not strictly necessary, though it remains a best practice for transparency.

### 5. Next Steps and Action Items

1.  **Immediate (by 16 November 2024):** mr. L. Okafor will draft the formal notification letter to the Dutch DPA for review by Maaslicht Retail B.V.’s board of directors.
2.  **Deadline Compliance:** Ensure the notification is submitted no later than **72 hours** after initial awareness (17 November 2024 at 09:30).
3.  **Monitoring:** Continue monitoring for any signs of misuse of the leaked data. Maaslicht Retail B.V. has been advised to offer credit monitoring services to affected customers as a goodwill gesture, regardless of the final legal determination on Article 34 notification.
4.  **Documentation:** All steps taken must be documented in the breach register in accordance with Article 30(2) GDPR for accountability purposes.

This note reflects our professional judgment that strict adherence to the 72-hour reporting window is critical to mitigate regulatory penalties, while the assessment of data subject notification remains an active legal evaluation pending forensic confirmation.

---