---
doc_id: meridiaan-0119-opinion-001
matter: 2025.0119
doc_type: opinion
title: Opinion on the necessity of a Data Protection Impact Assessment for the Meridiaan Media B.V. Audience Profiling Project
date: 2025-01-15
author: Meridiaan Media B.V.
recipient: mr. L. Okafor
privileged: true
parties: [Meridiaan Media B.V.]
statutes_cited: [Article 35 GDPR]
summary: Legal opinion assessing whether the intended large-scale processing of special category data for audience profiling requires a Data Protection Impact Assessment under Article 35 GDPR.
---
# LEGAL OPINION

**To:** Meridiaan Media B.V.
**From:** mr. L. Okafor, Privacy / GDPR Practice Group, Meridiaan Advocaten B.V.
**Date:** 15 January 2025
**Reference:** Matter 2025.0119
**Subject:** Assessment of the necessity for a Data Protection Impact Assessment (DPIA) regarding the "Audience Profiling Project"

## 1. Introduction and Scope of Advice

Meridiaan Media B.V. ("the Client") has requested legal advice from Meridiaan Advocaten B.V. regarding its planned launch of the "Audience Profiling Project." The objective of this project is to create detailed behavioral profiles of users visiting the Client's news portal *De Dagblader* (domain: dedagblader.nl) by combining browsing behavior with inferred sensitive interests.

The purpose of this opinion is to determine whether this intended processing operation constitutes a high risk to the rights and freedoms of natural persons, thereby triggering the mandatory obligation to conduct a Data Protection Impact Assessment (DPIA) pursuant to Article 35 GDPR.

This opinion is prepared in confidence (*privileged: true*) and forms part of the Client's internal compliance documentation. It addresses the specific legal requirements under Dutch implementation of the General Data Protection Regulation (GDPR).

## 2. Facts of the Matter

The processing activities described in this opinion are as follows:

1.  **Data Controller:** Meridiaan Media B.V., located at Kennedyplein 200, 5611 AP Eindhoven, Netherlands.
2.  **Processing Activity:** Automated profiling of website visitors using first-party cookies and server-side logging.
3.  **Data Subjects:** Visitors to *De Dagblader*, estimated at 45,000 unique users per month.
4.  **Categories of Data Collected:**
    *   Standard technical data (IP addresses, device identifiers, timestamps).
    *   Content consumption data (articles read, time spent per article, scroll depth).
    *   Inferred sensitive data: The profiling algorithm infers political opinions, religious beliefs, and health-related interests based on the specific categories of articles consumed. For example, frequent reading of articles regarding mental health clinics or specific political party manifestos is categorized as "health" or "political opinion."
5.  **Purpose:** To deliver hyper-targeted advertising to third-party advertisers who pay for access to these specific segments.
6.  **Retention Period:** The profiling data is retained for 24 months before being anonymized.
7.  **Risk Context:** The Client intends to sell these inferred profiles to external ad-tech partners via an API integration.

## 3. Legal Framework: Article 35 GDPR

Under Article 35(1) GDPR, a Data Protection Impact Assessment is required where a type of processing in particular using new technologies, and taking into account the nature, scope, context and purposes of the processing, is likely to result in a high risk to the rights and freedoms of natural persons.

The requirement is mandatory if the processing involves:
*   Systematic and extensive evaluation of personal aspects based on automated processing, including profiling, which produces legal effects or similarly significantly affects the data subject (Article 35(3)(a) GDPR); or
*   Processing on a large scale of special categories of data referred to in Article 9(1) (which includes political opinions, religious beliefs, and health data) or personal data relating to criminal convictions and offences (Article 35(3)(b) GDPR).

## 4. Assessment of Risk Factors

We apply the assessment framework provided by the European Data Protection Board (EDPB) Guidelines 04/2019 on DPIAs to the facts presented.

### 4.1. Evaluation of Automated Profiling (Article 35(3)(a))

The Client's project involves **systematic and extensive evaluation** of personal aspects. The algorithm does not merely categorize users into broad demographics but creates granular profiles based on real-time behavior. This constitutes "profiling" as defined in Article 4(4) GDPR.

While the primary purpose is commercial advertising rather than producing legal effects (such as loan denial), the EDPB guidelines indicate that profiling for direct marketing can still be high-risk if it leads to discrimination or exclusion. The significant effect here is the potential for manipulation of user behavior through targeted content, which may undermine the autonomy and informed consent of the data subjects.

### 4.2. Processing of Special Category Data (Article 35(3)(b))

This factor is decisive. The Client infers **special category data** (political opinions, health interests) from ordinary browsing behavior. Even though this data is not explicitly provided by the user, it is derived through sensitive inference algorithms.

1.  **Nature of Data:** Inferred political and health data falls under Article 9(1) GDPR. Processing such data is generally prohibited unless a specific exemption applies (e.g., explicit consent). The mere act of inferring these categories creates a high risk because users are typically unaware that their reading habits reveal their private beliefs or medical conditions.
2.  **Scope:** With 45,000 monthly active users, the processing is on a **large scale**.
3.  **Context:** The context involves a news website where users expect editorial content, not pervasive surveillance for advertising purposes. This creates an imbalance of power and expectation, increasing the perceived risk to data subjects.

### 4.3. Identification of Risks to Data Subjects

The combination of these factors yields the following high risks:
*   **Discrimination:** Advertisers or third parties could use these profiles to exclude individuals from opportunities (e.g., housing, insurance) based on inferred health or political status.
*   **Loss of Anonymity:** When combined with other datasets held by ad-tech partners, the profiles may become re-identifiable.
*   **Chilling Effect:** Knowledge that browsing habits reveal sensitive personal traits may deter users from accessing legitimate news content regarding controversial topics.

## 5. Conclusion and Advice

Based on the assessment above, the intended processing operation for the "Audience Profiling Project" is likely to result in a high risk to the rights and freedoms of natural persons. Specifically, it involves the large-scale processing of special category data (inferred political and health interests) via automated profiling.

Therefore, **a Data Protection Impact Assessment (DPIA) is mandatory under Article 35 GDPR.**

### Recommended Next Steps:

1.  **Conduct the DPIA:** The Client must draft a DPIA detailing the processing operations, purposes, risks, and mitigating measures. This should be done in consultation with the Dutch Data Protection Authority (Autoriteit Persoonsgegevens) if residual risks remain high after mitigation.
2.  **Legal Basis Review:** The Client must establish a valid legal basis for processing special category data. Consent under Article 6(1)(a) GDPR must be specific, informed, and unambiguous. Pre-ticked boxes or buried clauses in the privacy policy are insufficient. Given the sensitivity, explicit opt-in mechanisms are required.
3.  **Data Minimization:** Consider whether the inference of sensitive categories is strictly necessary for the advertising purpose. If the Client can achieve similar commercial results using non-sensitive demographic data, the legal risk decreases significantly.
4.  **Documentation:** The completed DPIA and the decision-making process must be documented to demonstrate accountability under Article 5(2) GDPR.

We are available to assist in drafting the DPIA template and reviewing the consent mechanisms prior to launch.

Respectfully submitted,

mr. L. Okafor
Meridiaan Advocaten B.V.
Kennedyplein 200
5611 AP Eindhoven
The Netherlands