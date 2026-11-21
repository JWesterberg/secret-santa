# Secret Santa

Draw names and email each participant who they got. The organizer never sees the assignments.

## Run

Requires Python 3.9+ (no packages to install).

```
python3 app.py
```

Open http://localhost:8000.

1. Add participants (name, email, optional exclusion group so couples don't draw each other).
2. Write the subject and directions. Placeholders: `{name}`, `{match}`, `{event}`.
3. Enter SMTP credentials for the sending account.
4. Send yourself a test (uses a fake match), then click **Draw names & send emails**.

You'll see only which addresses were sent successfully. If any fail, click **Retry failed emails**.
Don't redraw after a partial send: people who already got emails would have stale matches.

## Keeping it secret from yourself

- Assignments exist only in memory during the send. Nothing is logged or saved.
- **Sending from your personal Gmail puts every assignment in your Sent folder.** Use a throwaway
  Gmail account: enable 2-step verification, create an app password at
  https://myaccount.google.com/apppasswords, and use that as the password with `smtp.gmail.com` port 587.
- Don't add yourself with your own address to the "test" field and then also as a participant
  if you're worried about mixing them up. Test emails are labeled `[TEST]`.
