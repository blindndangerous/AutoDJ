# How songs become the next thing you hear

AutoDJ answers two separate questions: which song should come next, and how
should the station hand over to it? **Song picking** answers the first.
Transitions, cue points and inserted speech answer the second. Changing a
crossfade does not change the song pool.

## The queue comes before automatic picking

The **queue** holds deliberate choices, including manually added songs and
prepared show songs. A **show** is a saved ordered programme that can put its
songs into the queue. When queued material finishes, the station resumes its
automatic picker. A prepared next song may already be committed to playback;
an edit to future picking does not necessarily replace that audio.

This explains a common surprise: activating a clockwheel does not empty the
queue. If a queued song plays next, that alone does not show that the wheel
failed. Check the queue and the selected pick mode together.

## Each pick mode answers a different question

| Pick mode | Question it answers |
| --- | --- |
| Similarity | What eligible song sounds close to the selection being followed? |
| Random walk | What eligible song can I choose at random? |
| Least recently played | What eligible song has waited longest? |
| Clockwheel | What does the current step of my repeating pattern ask for? |

A **category** is a named pool of songs. A **clockwheel** repeats an ordered
list of steps, such as choosing from one category and then another. Saving a
wheel, activating it, and selecting **Clockwheel** as **Pick mode** are separate
choices. Categories also have uses outside clockwheels; creating one does not
force every pick mode to use it.

Similarity needs **advanced analysis**, saved information about how a song
sounds. Basic import can make a song playable without giving it that analysis.
A song can therefore appear in search and play from the queue while still
being unavailable as a normal similarity candidate. The analysis model measures
relationships; it does not understand your station's taste or promise a good
musical transition. Discovery deliberately introduces more distant choices.

## Rules shape a choice without writing a playlist

Repeat rules hold back recently played songs, artists and other configured
relationships. Category conditions, time restrictions, tempo and key settings
can narrow the pool further. These constraints can compete, especially in a
small library. Some automatic picking paths relax repeat restrictions rather
than stop the music. A clockwheel step can explicitly ignore repeat rules.
Manual choices bypass ordinary repeat avoidance; enabled DMCA checks have their
own final playback checks. Do not treat any rule as a legal guarantee.

**Fit songs to the hour** prefers a useful length among songs the picker would
already allow. It does not create a fixed playlist or make a queued song shorter.
Likewise, a description of a pick's tempo, key or energy describes a relationship;
it is not a complete audit of every reason that candidate won.

Use [Build your sound](../guides/build-your-sound.md) to change selection,
[the similarity lesson](../tutorials/similarity-picking.md) to try it, and
[the settings reference](../settings-reference.md) for exact values. Contributors
can read the [complete algorithm and exceptions](../contributor/track-selection.md)
and [model evaluation](../contributor/model-selection.md). The
[picking and preview ADR](../adr/0009-share-picking-rules-with-isolated-previews.md)
is Proposed; it records implementation evidence rather than a newly accepted policy.
