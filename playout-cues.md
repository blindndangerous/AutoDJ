**Points for the supplied audio**

The [playout guide](playout.md) describes how these are used. These music points come from the accepted round-nine recordings (`local/playout-round9/index.html`, local only), with exact source details in results.json (`local/playout-round9/results.json`, local only). Times refer to positions in the original file. A dash means a point has not been supplied; for playback, an unset Intro gives no intro time and an unset Outro gives no early talk-over time.

| Track | Start | Intro | Outro | Segue | End |
| --- | --- | --- | --- | --- | --- |
| Outkast — Ms. Jackson | 0:00.000 | — | 3:50.000 | 3:52.000 | 4:02.207 |
| Kendrick Lamar — Not Like Us, radio edit | 0:00.000 | 0:04.000 | — | 4:30.600 | 4:31.804 |
| PinkPantheress — Boy's a Liar, radio edit | 0:00.000 | 0:00.000 | — | 2:10.000 | 2:10.743 |
| Arizona Zervas — Roxanne, supplied extended clean edit | 0:00.000 | 0:09.000 | 2:24.000 | 2:35.000 | 2:39.347 |
| Central Cee — Doja, radio edit | 0:00.000 | 0:02.000 | 1:23.000 | 1:28.000 | 1:37.358 |
| Iyaz — Replay, J-Squared intro edit | 0:00.000 | 0:11.000 | — | 3:10.500 | 3:12.549 |
| Blackstreet — Don't Leave Me | 0:00.000 | 0:01.500 | — | 4:07.000 | 4:18.456 |
| White Town — Your Woman | 0:00.250 | 0:12.000 | 4:17.000 | 4:16.000 | 4:17.097 |
| Jordan Knight — Give It to You, radio edit without waltz | 0:00.000 | 0:08.000 | — | 3:48.500 | 3:48.754 |

White Town starts at 0:00.250, leaving 11.75 seconds before its 0:12 vocal. Keep its approved 4:16 segue even though its stored Outro is later. The End column includes the remaining file/tail; the song's fade can finish sooner. The original Outkast and Blackstreet segue suggestions came from their ending levels and were retained in the accepted examples.

**All the following are Voice liners.** Each has one **Overlap start** field. Zero means fit the complete liner around the song cues; it does not force the next song to start immediately. A positive value releases the next song that many seconds after the liner's selected Start, allowing its remaining audio to overlap even an incoming vocal. End still controls the full playback length. No separate liner Outro or fade point is required; the music table above is unchanged.

| Recording | Start | Overlap start | End |
| --- | --- | --- | --- |
| IMAGING — ALL DAY | 0:00.000 | 0:00.000 | 0:04.832 |
| IMAGING — WE'LL BRING THE PARTY | 0:00.000 | 0:00.000 | 0:05.564 |
| Plague 25 | 0:00.000 | 0:05.300 | 0:08.809 |
| vt_nfbco.wav | 0:00.000 | 0:00.000 | 0:18.283 |
| vt_christmas.wav | 0:00.400 | 0:00.000 | 0:09.092 |

The Christmas recording retains 8.692 seconds after its selected Start; internal pauses remain. Plague's 5.3-second overlap start allows even a cold song to enter there and retains reverb to 8.809 seconds. Ordinary voices and IDs stay at the default zero and are fitted to the available outro/intro window.

The Overlap start column describes the implemented timing rule. The older round-nine examples still contain ID/voice differences, including a quarter-second cold-entry allowance. The station implementation uses the shared rule without those exceptions.
