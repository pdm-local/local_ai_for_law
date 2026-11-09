---
doc_id: meridiaan-0133-note-001
matter: 2025.0133
doc_type: attendance_note
title: Breach assessment and notification strategy for Maaslicht Software incident
date: 2025-04-15
author: mr. L. Okafor
recipient: 
privileged: true
parties: [Maaslicht Software B.V., Dutch DPA]
statutes_cited: [Article 33 GDPR]
summary: This note records the legal assessment of a data breach affecting Maaslicht Software B.V. and outlines the mandatory notification obligations to the supervisory authority under Article 33 GDPR.
---
**ATTENDANCE NOTE**

**Matter:** 2025.0133 – Maaslicht Software B.V. Data Breach Response
**Date:** 15 April 2025
**Author:** mr. L. Okafor, Privacy / GDPR Specialist
**Client:** Maaslicht Software B.V., Keizer Karelshof 42, 5611 TP Eindhoven

**1. Context and Incident Overview**

On 13 April 2025 at 09:15 CET, the IT security team of Maaslicht Software B.V. detected unauthorized access to their customer relationship management (CRM) database. The incident involved a potential exfiltration of personal data belonging approximately 4,200 users. The data included names, email addresses, hashed passwords, and IP addresses. No financial data or health-related special category data was compromised.

Maaslicht Software B.V. engaged our firm immediately to assess the severity of the breach and determine the necessary compliance steps under the General Data Protection Regulation (GDPR).

**2. Legal Assessment: Notification to the Supervisory Authority**

Pursuant to **Article 33 GDPR**, a personal data breach must be notified to the competent supervisory authority without undue delay and, where feasible, not later than 72 hours after having become aware of it.

The firm has evaluated whether this incident qualifies as a breach likely to result in a risk for the rights and freedoms of natural persons. Given that the passwords were hashed but not salted adequately due to a recent configuration error, there is a discernible risk of unauthorized access to user accounts. Furthermore, the exposure of email addresses and IP addresses allows for potential profiling or phishing attacks. Consequently, this breach meets the threshold for mandatory notification.

The firm calculates the timeline as follows:
*   **Awareness:** 13 April 2025, 09:15 CET.
*   **Deadline:** The 72-hour window expires on 16 April 2025 at 09:15 CET.

It is critically important to submit the notification within this strict **72 hours** period. Failure to do so without valid justification may result in administrative fines under Article 83 GDPR. We have drafted the requisite report for submission to the Dutch Data Protection Authority (Autoriteit Persoonsgegevens). The report details the nature of the breach, the likely consequences, and the measures taken to mitigate the risk.

**3. Assessment: Communication to Data Subjects**

Under **Article 34 GDPR**, personal data subjects must be informed without undue delay when the breach is likely to result in a high risk to their rights and freedoms.

In this matter, while there is a risk of account compromise, Maaslicht Software B.V. has implemented immediate remedial measures:
1.  Forced password resets for all affected users.
2.  Mandatory two-factor authentication (2FA) enrollment upon next login.
3.  Temporary suspension of API access for suspicious IP ranges.

Given these mitigating controls, the risk to the data subjects is significantly reduced. However, due to the lack of proper salting in the password storage, a residual risk remains. We advise Maaslicht Software B.V. to send a direct communication to the affected data subjects explaining the incident, the potential risks, and the steps they should take (e.g., changing passwords on other platforms if reused). This communication must be clear and plain language.

**4. Action Plan and Next Steps**

1.  **Submission to Authority:** mr. L. Okafor will submit the breach notification to the Dutch DPA on behalf of Maaslicht Software B.V. no later than 16 April 2025 at 08:00 CET, ensuring compliance with the **72 hours** deadline stipulated in **Article 33 GDPR**.
2.  **Data Subject Notification:** Draft a template notification letter for client review to be sent to all 4,200 affected users by 18 April 2025.
3.  **Documentation:** Ensure all details of the breach, including the assessment of risk and the measures taken, are documented in Maaslicht’s internal record of processing activities as required by Article 30 GDPR.

**5. Conclusion**

The incident constitutes a reportable personal data breach. The primary obligation is to notify the supervisory authority within the statutory **72 hours**. Secondary obligations regarding data subject notification depend on the residual risk after mitigation, which we advise treating as high due to the password hashing deficiency.

mr. L. Okafor