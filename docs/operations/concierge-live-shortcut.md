# Install BLAM8 Concierge

After deployment, open `https://blam8.com/concierge/live` in Safari while signed
in. Share > Add to Home Screen, with Open as Web App enabled, installs
**BLAM8 Concierge**. This is a separate installation from the normal BLAM8 app;
existing BLAM8 installations keep their dashboard start page. Installing from
`/app/concierge` still installs the normal app, so use the exact live address.

The live address renders the shared protected Concierge page without redirecting.
Its dedicated manifest has identity and start URL `/concierge/live`, so cold
launch and refresh select the existing voice launcher without needing a fragment.
The normal manifest, text Concierge, and existing shortcuts remain unchanged.

Assign a Shortcut opening the installed BLAM8 Concierge web app to the iPhone
Action Button where iOS exposes it; alternatively use Open URL with the live
address. Verify the chosen Shortcut on the device: URL actions may open Safari
rather than the installed app. The server cannot force iOS's app selection.

The launch uses the selected voice mode and existing Launch by button preference.
It never changes microphone permissions or forces automatic listening; a tap may
still be required. Sign-in, onboarding, subscription and Concierge entitlement
checks still apply. If asked to sign in, finish sign-in then reopen the Concierge
icon. On-device installation, cold launch and Action Button checks remain release
acceptance steps; desktop/offline tests do not prove iOS integration.
