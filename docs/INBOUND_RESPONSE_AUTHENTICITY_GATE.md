# Inbound Job Response Authenticity Gate

Use this gate before replying to, clicking links from, or sharing information with any recruiter, company, staffing agency, or interview invitation.

## Intake record

Record only non-secret metadata:

- received timestamp
- sender display name and full email/domain
- company name
- role title
- claimed requisition or application ID
- channel: email, SMS, LinkedIn, job board, phone, other
- matching application receipt or tracker entry
- link hostnames shown in the message, without opening them
- requested action and deadline

## Gate

### 1. Application match

- Match the company, role, and requisition to a real tracked application.
- A missing match is not automatic fraud, but status remains **UNVERIFIED**.
- Never accept "we found your resume" as proof.

### 2. Independent company verification

- Find the official company website independently; do not use the message link.
- Confirm the sender domain exactly matches the official company or a documented recruiting vendor.
- Treat free-email accounts, misspellings, extra hyphens, substituted characters, and unrelated domains as red flags.

### 3. Recruiter verification

- Confirm the recruiter through the company's official staff page, verified LinkedIn presence, or official HR/recruiting contact.
- Contact the company using a number or address found independently, never one supplied only in the suspicious message.

### 4. Process sanity

Mark **SCAM LIKELY** if the message asks for any of these before verified onboarding:

- money, fees, gift cards, crypto, equipment purchases, check deposits, or reimbursements
- bank credentials, card numbers, passwords, authentication codes, or remote-computer access
- SSN, passport, driver's license, or tax forms through ordinary email/text
- immediate hiring without a normal interview
- conversation moved only to Telegram, WhatsApp, Signal, or another private messenger
- pressure, secrecy, threats, or an artificial same-day deadline

### 5. Link and attachment safety

- Compare the visible hostname with the official domain character by character.
- Do not open shortened URLs, unexpected login pages, macros, executables, archives, or unknown cloud-share attachments.
- When uncertain, navigate independently to the official ATS or company careers page.

## Decision

- **VERIFIED:** application matches, sender/recruiter independently confirmed, request is normal, and destination is official.
- **UNVERIFIED:** evidence is incomplete or inconsistent. Do not reply, click, download, schedule, or share personal information.
- **SCAM LIKELY:** one or more high-risk requests or impersonation indicators. Preserve the message metadata and stop.

## Minimum verification receipt

A response can be treated as genuine only when the record contains:

1. matching application or independently confirmed sourcing path;
2. official company/recruiting-vendor domain;
3. independently verified recruiter or official HR confirmation;
4. official interview/scheduling destination;
5. no payment, credential, identity-document, check-deposit, or remote-access request.

## Automation boundary

KD Job Hunter may classify and summarize inbound responses. It must not reply, click, download, schedule, submit documents, share personal data, or contact a third party without explicit authorization.
