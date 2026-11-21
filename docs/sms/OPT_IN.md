# Vigil Alerts: How Consent (Opt-In) Is Collected

**Opt-in type: verbal, in person.** Vigil Alerts has a few recipients: the account owner and members of their own household (no more than 5). There is no public sign-up, web form or keyword, and numbers are never obtained from lists or third parties.

## Process
1. **Ask in person.** Before adding anyone, the account owner explains the program using the script below and asks whether the person wants to receive alerts.
2. **Agreement.** The person's number is added only if they clearly say yes.
3. **Record.** The owner enters the number in the private Vigil admin console, which time-stamps the entry. Adding a number is restricted to the household administrator and is written to an audit log.
4. **Ongoing control.** Recipients can reply **STOP** at any time to opt out (handled automatically, with a single confirmation), **START** to opt back in, or **HELP** for help.

## Consent script (read to each recipient)
> "I'd like to add your mobile number to Vigil Alerts, our home pet-monitoring system. You'd get a text if the camera sees possible seizure-like movement or possible vomiting in our pet, up to two reminders if nobody responds, and nothing else: no marketing. It's usually 0 to 10 texts a month. Message and data rates may apply. You can reply STOP at any time to stop them, or HELP for help. The terms and privacy policy are at github.com/lawrenceDegoma/Vigil/tree/main/docs/sms. Do you agree to receive these texts?"

## Sample messages
> Vigil URGENT: possible seizure-like movement seen for [pet name] at 9:41 PM (Living room). Please check now.

> Vigil reminder 1/2: possible seizure-like movement for [pet name] at 9:41 PM has not been acknowledged.

> Vigil: possible vomiting seen for [pet name] at 9:41 PM (Living room).

> Vigil: Living room is OFFLINE. No heartbeat since 9:30 PM. Monitoring and alerts may not be working.

## Related
- [SMS Terms and Conditions](TERMS.md)
- [Privacy Policy](PRIVACY.md)

*Last updated: 2026-11-21.*
