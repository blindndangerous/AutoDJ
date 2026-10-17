# Take and moderate a Practice song request

You will send a song request as a listener, approve it as the operator, and
watch it join Practice's queue. You will learn why a request received is
different from a request accepted, and why acceptance is not an exact airtime.

Start with [Build your first station](first-station.md) complete on a practice
station with no audience: at least five analyzed songs, the Practice category
and single-step Practice clockwheel, and Clockwheel pick mode. Browser output
is enough for this lesson. If you kept Radio stream output after
[listening on another device](stream-second-device.md), use its instructions
to connect your test listener when it is time to hear the result.

## Before you begin

Use the default Practice station so its listener page is `/requests`. Practice
means your first lesson's station; its displayed name may still be AutoDJ. You need
the operator page and a separate browser tab for the listener page on the same
AutoDJ computer. A private browsing window makes the difference between
listener and operator clearer; the request page does not require pairing.

No second device, paid service or LAN exposure is needed. If your existing
server is accessible over the network, enabling requests makes this public
page available to anyone who can reach that address. It gives them request
capability, not station controls. Do not publish its link for this practice.

You will save request settings, submit two requests, approve one and turn down
the other. Requests and decisions persist separately from the music library;
turning requests off keeps waiting entries. Use invented names rather than
personal information. Note your original request settings so you can restore
them afterward.

Keep **Play or Pause** paused while preparing the Browser station. In stream
mode, stop your own listening with **Listen here** or your network player;
closing a listener does not immediately stop the server's set. Use Tab to
reach controls, Enter or Space to operate them, and the
[keyboard guide](../guides/keyboard-and-screen-reader.md) for table navigation.

## Prepare an uncomplicated request slot

1. Select that default station with the **Station** button if several stations exist.
2. Open **Queue & Search**. Use **Clear queue** and confirm if there are entries.
3. Check **Settings**, **Song picking**: **Pick mode** should be **Clockwheel**.
4. In **Library tools**, **Clockwheels**, inspect Practice's single music step.
   It should choose the Practice category with **Follow the repeat rules**
   unchecked, as in the first lesson, and have no day or hour restriction.
5. Return to **Queue & Search** and search for a song in your practice folder
   that is neither playing nor coming up. Note its title.

Choose only a song from that folder. An active clockwheel still constrains
requests: a requested song must fit the next music step. This lesson's broad
Practice step avoids an unrelated category restriction. Do not disable DMCA
mode or other station access and compliance settings to force acceptance.
Those checks can still restrict a request; choose an eligible song instead.

Confirm the queue is empty and note the currently playing and upcoming titles.
That gives you a before-state against which to check the request's insertion.
If a scheduled show is present, finish or postpone this practice until it is
out of the request window; this lesson assumes no show or scheduled intervention.

## Open requests with manual approval

1. In **Queue & Search**, expand **Listener requests**.
2. Under **Request settings**, check **Take listener requests**.
3. Under **Approval**, select **I approve each one**.
4. Leave **Add bridge songs before a request that would sound abrupt** unchecked.
5. Press **Save request settings**.
6. In the listener tab, open the same server address with `/requests` as its
   path. For example, from `http://localhost:8080/`, open
   `http://localhost:8080/requests`.

The saved settings take effect immediately; no restart is needed. The listener
page should show a search form. If it says **Requests are closed**, return to
the operator tab, check you selected the intended station, and save the checked
**Take listener requests** control. Reload the listener page. Editing a checkbox
without saving does not open the page.

For a named station, the page would instead be
`/stations/<station-id>/requests`; `/requests` always targets the default.
Changing station in the control tab does not rewrite a listener tab's address.

## Send and inspect your first request

1. On the listener page, enter the noted song's title in **Title, artist or album**.
2. Press **Search**, then follow that song's link in **Search results**.
3. Enter `Practice Listener` in **Your name (optional)**.
4. Press **Send request** once.
5. Read the returned page's heading. With this eligible song and manual approval,
   it should say **Request received**.
6. Return to the operator tab's expanded **Listener requests** card. Allow its
   next refresh, up to about 15 seconds, and inspect **Waiting requests**.

Find your title and invented name. Read the placement explanation or the
restriction beside it. The received page proves the submission is waiting;
it does not prove the song joined the queue. Check that difference by looking
at the queue before approving.

If the result says **Already on the way**, choose a different Practice song
that is neither current, upcoming nor already waiting. If it says **Not right
now**, inspect the operator's restriction and choose another eligible song;
repeat and DMCA checks still apply. Avoid repeatedly resubmitting the same
form: request limits also apply. An expired form needs a fresh search and
confirmation, rather than another press of its old button.

## Approve and hear the result

Find your waiting entry and press **Approve**. Read the result and
**Recent decisions**, then inspect the queue for that exact title. Approval
checks placement again at that moment; a request that no longer fits can stay
waiting. If **Approve** is unavailable, read the reason, resolve the actual
restriction, and use a different eligible song if necessary. Do not substitute
**Play next** simply to bypass the lesson's placement check.

Resume Browser playback with **Play or Pause**, or connect your stream test
listener. Let the current song finish and watch for your requested title in
**Now Playing**. Listen if possible and inspect **History** after it plays.
Let another song follow to see Practice return to automatic rotation.

The queue and History supply text evidence alongside listening. A paused
Browser player will not consume the request until you resume it. In stream
mode, an absent listener may leave the station idle. Check these states before
treating a queued request as stuck. A request's placement can also change when
an operator adds songs or a show starts, so do not promise a precise airtime.

## Try it: close requests while one waits

Send a second eligible, different Practice song through a fresh listener search.
Confirm **Request received** and its waiting entry. Predict whether switching
requests off will delete that entry or queue it automatically.

Uncheck **Take listener requests**, press **Save request settings**, and reload
the listener page. It should say **Requests are closed**. Turn requests on and
save again, then inspect the waiting list.

Expected result: the unapproved request remains waiting, unless its three-hour
expiry has passed. Closing the page accepts no new requests and does not turn
waiting requests into approved songs. Find that entry and press **Turn down**.
Check **Recent decisions** and confirm it did not join the queue. Explain why
closing requests and moderating a received request are separate actions.

## Stop and restore Practice

Uncheck **Take listener requests** and press **Save request settings**. Reload
the listener page once more to check that requests are closed. Turn down any
remaining test entries first if you do not want them waiting for a later session.
Closing requests does not remove already queued songs; remove a remaining test
song from the queue explicitly if it has not played.

Pause Browser playback and check that elapsed time stops. For Radio stream,
disconnect your test listeners and follow the
[stream lesson's cleanup](stream-second-device.md#stop-and-restore-browser-practice).
Restore any request settings you changed, leaving requests closed for practice.
Decision history remains saved; this lesson does not erase it or the songs.

You can now distinguish receiving, approving, queuing and playing a request.
Use [Go live and take requests](../guides/going-live.md) for other workflows,
or the [request reference](../operations.md#listener-requests) for limits and placement.

AutoDJ and this page were developed with AI assistance.
