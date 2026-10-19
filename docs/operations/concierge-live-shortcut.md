# Install BLAM8 Concierge

After deployment, open `https://blam8.com/concierge/live` in Safari while signed
in. Share > Add to Home Screen, with Open as Web App enabled, installs
**BLAM8 Concierge**. This is a separate installation from the normal BLAM8 app;
existing BLAM8 installations keep their dashboard start page. Installing from
`/app/concierge` still installs the normal app, so use the exact live address.

The live address renders the shared protected Concierge conversation without
redirecting. It has a dedicated shell without platform navigation, while keeping
the same history, action cards and extended-screen panel used by the normal
Concierge page. Platform record links open separately so the installed Concierge
stays available. Its dedicated manifest has identity and start URL
`/concierge/live`, so cold launch and refresh select the existing voice launcher
without needing a fragment. The normal manifest, text Concierge and existing
shortcuts remain unchanged.

The Concierge settings cog is available on the dedicated conversation and both
full-screen voice modes. Opening settings ends the current voice session before
the microphone meter starts. The saved voice choice, including GPT-Live, is read
by the next voice launch; availability and microphone permission checks still
apply. The existing modal header cog uses the same settings sheet.

Assign a Shortcut opening the installed BLAM8 Concierge web app to the iPhone
Action Button where iOS exposes it; alternatively use Open URL with the live
address. Verify the chosen Shortcut on the device: URL actions may open Safari
rather than the installed app. The server cannot force iOS's app selection.

The launch uses the selected voice mode and existing Launch by button preference.
It never changes microphone permissions or forces automatic listening; a tap may
still be required. Sign-in, onboarding, subscription and Concierge entitlement
checks still apply. If asked to sign in, finish sign-in, then reopen the
Concierge icon; the sign-in flow may leave the installed app and require that
relaunch. iOS decides whether an Action Button Shortcut opens Safari or the
installed web app. On-device installation and Action Button behavior have not
been verified by desktop/offline checks.

## iPhone acceptance checklist

- Install **BLAM8 Concierge** from `/concierge/live` and confirm the normal BLAM8
  installation remains separate.
- Cold-launch the Concierge icon and refresh; both should return to
  `/concierge/live`.
- Open Settings from the text screen, Standard voice, and GPT-Live voice. Confirm
  the voice session closes before the settings mic meter opens.
- Select GPT-Live, close Settings, then launch voice and confirm the selected
  available mode is used.
- Swipe to the extended Concierge screen and back; verify platform record links
  open separately and the installed screen stays put.
- Open normal BLAM8 and confirm its platform navigation and `/app/` start remain
  unchanged. Test the Action Button Shortcut on the device if it is configured.
