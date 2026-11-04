# Hospital Auto-Apply (personal version)

Your own private robot on a small cloud server. Every hour it checks the hospital careers sites for PSA and nursing jobs. For each good match it:

1. scores the job against your resume (Claude AI) and writes a cover letter,
2. signs in with **your** hospital account and fills in the application, **without submitting**,
3. emails you a screenshot: **"Ready to submit"**.

You open the link, check it, and tap **Approve & submit**. That's your only job.

It stops and asks you whenever it isn't sure. Factual questions such as "Do you have a Working With Children Check?" are never guessed: you answer them once and they're reused. It never solves CAPTCHAs and never submits the same job twice. If it can't confirm a submission went through, it says **check needed** and never retries by itself.

> These are your own accounts, used for your own applications. Hospital sites have their own terms of use. Keeping the approval step (the default) means a person checks every application.

---

## Setup (about 20 minutes, all on your phone)

### 1. Get two keys first
- **Claude API key:** console.anthropic.com → API Keys → **Create key**, and add about US$10 of credit. Copy the key (it starts with `sk-ant-`).
- **Gmail app password** (lets the robot email you): myaccount.google.com → Security → turn on **2-Step Verification** → search **App passwords** → create one called `autoapply`. Copy the 16 letters.

### 2. Create the install script in Vultr
1. Sign up at **vultr.com**.
2. Go to **Orchestration → Scripts → Add Startup Script**. Name it `autoapply`, type **Boot**, and replace the example with these two lines. Change `pick-a-code` to a code of your own (e.g. `blue-kangaroo-42`):
```bash
#!/bin/bash
curl -fsSL https://raw.githubusercontent.com/ardentluma01-spec/hospital-autoapply/main/install.sh | SETUP_CODE=pick-a-code bash
```
3. Tap **Add Script**.

### 3. Deploy the server (about US$12/month)
**Deploy → Cloud Compute – Shared CPU**, location **Melbourne**, image **Ubuntu 24.04**, plan **2 GB RAM**. Under **Server Settings → Startup Script**, choose `autoapply`. Deploy.

### 4. Wait about 10 minutes, then open your dashboard
Copy the server's **IP address** from Vultr (e.g. `45.76.1.2`). Your dashboard address is that IP with **dashes** instead of dots, then `.sslip.io`:

**`https://45-76-1-2.sslip.io`**

If it doesn't load yet, wait a few more minutes; the server is still installing.

### 5. One-time setup page
The first time, the dashboard shows a setup page. Enter:
- your **setup code** from step 2
- a **dashboard password** you make up (10+ characters)
- the **Claude key** and **Gmail** details from step 1

Tap **Save and start**, then log in with user **`me`** and your new password.

### 6. Fill in your details
1. **Your resume:** upload a PDF or Word file.
2. **Your profile:** fill in every box, especially work rights, Cert III, WWCC and availability.
3. **Hospital logins:** enter the email and password you already use on each hospital careers site → **Save** → **Test login**. Refresh after about 30 seconds; it should say **login works** (tap **See screenshot** to check).
4. Tap **Check for jobs now**. After that it checks every hour by itself.

Tip: **Share → Add to Home Screen** makes it open like an app.

## Day to day

| You get an email saying… | What to do |
|---|---|
| **Ready to submit (88%)** | Open the link, look at the screenshot and cover letter (fix anything flagged), then tap **Approve & submit** |
| **Needs you** | It hit a question only you can answer. Answer it on the job page and it re-fills the form |
| **Applied** | Done. The hospital site confirmed it |
| **Please check** | It clicked Submit but didn't see a confirmation. Check your account on the hospital site, then tap **it WAS** / **it was NOT submitted** |

The **Approve** button only appears when a form is fully filled. Each job can be submitted only once.

## Settings (`config.yaml`)
`nano /opt/autoapply/config.yaml`, then `systemctl restart autoapply`:
- `include_keywords` / `exclude_keywords`: which job titles to look at
- `min_match_score`: how good a match must be (default 65)
- `hospitals`: switch each one `enabled: true/false`. Northern Health needs its careers address pasted in first; St Vincent's (Workday) only emails you links
- `mode: approve` (recommended). `auto` would submit without asking you.

## If something's wrong
Most problems can be checked from Vultr → your server → **View Console** (log in as `root` with the password shown on the server page):
```bash
cat /var/log/autoapply-install.log  # did the install finish? (last line says DONE)
journalctl -u autoapply -n 100     # recent log
systemctl restart autoapply         # restart
```
- **"login failed":** re-enter the password under Hospital logins. If the site asks for an emailed code, the robot can't do that; apply yourself for that hospital.
- **A form wasn't recognised ("blocked"):** apply yourself with **I applied myself**. Send me the screenshot and the log so the robot can be adjusted for that hospital.
- **Dashboard won't open:** `systemctl status caddy autoapply`.

## Privacy
Everything stays on your server: resume, answers, screenshots (deleted after 30 days) and your hospital passwords (encrypted). Your resume text and job ads are sent to Anthropic's Claude AI to score jobs and write letters.
