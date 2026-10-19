# Concierge live shortcut

Set an iPhone Shortcuts Open URL action to `https://blam8.com/concierge/live`
and assign that shortcut to the Action Button.

This URL redirects to the existing `/app/concierge#talk` launch flow. Normal sign-in,
onboarding and Concierge access checks still apply. It uses the selected voice
mode and the existing per-device Launch by button preference; it does not force
GPT-Live, change preferences, or bypass microphone permissions. If automatic
listening is disabled or the browser requires a gesture, tap the microphone.

The shortcut does not guarantee iOS will open the installed PWA instead of the
browser. Device/PWA launch and microphone behaviour need verification on iPhone.
