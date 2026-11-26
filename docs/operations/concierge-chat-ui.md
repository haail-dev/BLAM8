# Concierge chat presentation

The shared Concierge panel serves the page and mobile modal. Assistant messages
align left and owner messages align right. Both fit their text up to 95% width.
Tool activity is a left-aligned text trail, without pill borders or card fills.
Active words use a bright pink-and-blue text shine and stepping ellipsis;
completed words use a muted version of their Text, Live or VC group colour.
Each completed lookup stays tappable when it has lookup details. Entries are
comma-separated and the completed turn ends with a full stop. The initial
request status uses the same plain-text presentation.

Narrow page and mobile modal layouts retain the two-page swipe strip, with no
persistent edge arrows. Newly offered cards and task lists use Open now on the
right and reveal that page automatically. Returning focus to the composer must
not scroll the strip back to the chat. A card opened from Waiting remains in
the Waiting focus view. The existing desktop modal remains a single panel.

The narrow layout replaces the circular waiting shortcut with a compact Waiting
count between Explore actions and Clear history, centred below the composer.
It has a transparent fill and a slowly moving amber hazard border. Tapping it
reveals and expands Waiting for you. It disappears when there is nothing waiting.
Wide layouts retain their existing waiting shortcut and side-by-side column.

In Live Concierge, the circular bottom-right arrow or the Right Arrow key opens
the side page. The return control or Left Arrow key brings the voice screen
back. The voice controls slide out as the cards slide in over the same stationary
water and glow. The BLAM8 mark belongs to the voice orb. The same controls work in the full page and the modal.

When the voice screen opens the mobile side page, the page content clears the
iPhone safe areas on all four edges. Empty space in its transparent overlay
passes taps through to the voice background; the panel controls keep their own
touch targets. Settings sits at the top-left with a corrected, legible gear
icon. The Context button, context review overlay, and red round-count meter are
removed from the voice screen.

The public/onboarding Assistant's notice shine is broader, more angled and
slightly faster. It remains a text effect. Reduced motion stops the shine,
stepping dots and hazard border. Forced colours retain static readable tool text.

## Progress boundary

Typed foreground requests send tool-start events and one final response. These
provide the compact activity trail; they do not report task-group boundaries.
Background task batches run in priority order and expose task status through
polling, but do not stream each internal lookup into the chat. Completed voice
lookup trails share the presentation; this change adds no voice event transport.
No fabricated grouping, progress percentage or live background-tool feed is added.

## Local verification

Focused offline coverage lives in test_concierge_full_width.py,
test_column_on_phones.py, test_concierge_sidebar.py,
test_live_screen_links_and_waiting_focus.py, test_concierge_waiting.py and
test_concierge_transparency.py. Browser checks use synthetic messages and mocked
responses: saved and live layout, narrow wrapping, Waiting navigation, automatic
action reveal, tool-start/next-tool/completion, tappable lookup details, animation
and reduced motion. These are local checks, not deployment or tenant-data proof.
