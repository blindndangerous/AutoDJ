# Alternate between two song pools

You will divide Practice into two categories and make a clockwheel alternate
between them. By the end, you will be able to predict the next pool, then check
the prediction against the titles that actually played.

## Before you begin

Complete [Build your first station](first-station.md). Start with its imported
folder of at least five basically analysed songs, the folder-based `Practice`
category, and the one-step `Practice` clockwheel. Use **Browser** playback on
the same station, with no listeners. Advanced analysis is unnecessary.

If you followed another lesson, choose **Activate** from **Options for Practice**
in **Clockwheels**, then set
**Settings**, **Song picking**, **Pick mode** to **Clockwheel**. Its step should
still have **Follow the repeat rules** unchecked and no day or hour restrictions.
Use each lesson event's **Options** menu to **Disable** it, and stop any show before continuing.

This lesson saves two new categories and a new wheel, `Practice alternating`.
It keeps the original Practice category and wheel so you can return to them.
No music files are moved or changed. Allow several song lengths for observation.

Pause in **Now Playing** with **Play or Pause** while reading. Check that the
elapsed time stops advancing. Use Tab and Enter or Space for controls; use
Left and Right Arrow in tab strips. For the clockwheel table, see the
[keyboard and screen-reader guide](../guides/keyboard-and-screen-reader.md).
Every listening check below also has a title or saved-state check.

## Choose one recognizable song

Open **Queue & Search**, leave **Search text (optional)** empty, and press
**Search**. Choose a song whose title differs from the other practice songs.
Write down the complete title exactly as displayed, including punctuation.
Call it your *anchor song* for this lesson; do not enter the words “anchor song”
in place of its actual title.

The first pool will contain that title in the practice folder. The second will
contain everything else in that folder. A **condition** is a test a song must
pass to enter a pool. Two conditions joined with **Match all** must both pass.
This lets us divide the existing folder without depending on genre tags.

If several songs share the chosen title, choose another unique title. The next
stage's count is the check that this choice worked.

## Save the anchor pool

1. In **Library tools**, expand **Categories** and press **Add category**.
2. Set **Name** to `Practice anchor`. Under **Songs come from**, select
   **Matching conditions**. Set **Match conditions** to **Match all**.
3. In **Condition 1**, choose **Folder** as **Condition type**, your imported
   practice folder as **Library folder**, and **is** as **Operator**.
   Leave **Subfolder** empty and check **Include subfolders**.
4. Press **Add condition**. In **Condition 2**, choose **Title**, set
   **Operator** to **is**, and put the complete anchor title in **Value**.
5. Leave **When the songs may play** unrestricted: no selected days, times,
   or dates. Press **Check**.
6. Confirm that exactly one song matches, then press **Save category**.

Check **Saved categories** for Practice anchor and its count. If zero songs
match, edit the category form, compare **Value** with the library title, and
check the folder selection. Press **Check** again before saving. If more than
one matches, choose a unique title and replace **Value**. Do not continue until
this pool contains one song.

## Save the remaining songs as a second pool

1. Press **Add category** again and name it `Practice other`.
2. Select **Matching conditions** and **Match all**.
3. Set **Condition 1** to the same **Folder** condition, including
   **Include subfolders**, as the anchor pool.
4. Press **Add condition**. Set **Condition 2** to **Title**, **Operator**
   **is not**, and the same complete anchor title in **Value**.
5. Leave day, time, and date restrictions unset. Press **Check**, then
   **Save category** once the count is correct.

With five unique-title songs, Practice anchor should contain 1 and Practice
other should contain 4. With more songs, the second count will be larger.
Together the two counts should equal the original Practice category's count.

If Practice other includes everything, check that you selected **Match all**,
not **Match any**. Under Match any, belonging to the folder alone is sufficient;
the title exclusion would not divide it. Edit, correct that selection, check
again, and save. This is why checking counts comes before hearing the rotation.

## Build the alternating wheel

1. Expand **Clockwheels**, enter `Practice alternating` in **New clockwheel name**,
   and press **Create**. Check **Name** in the dialog, then press **Create**.
   Opening or saving a wheel leaves the panel's new-name field as you typed it.
2. Press **New step**. Set **Step type** to **Music matching conditions**.
3. Set **Condition 1** to **Category**, **Music category** to
   **Practice anchor**, and **Operator** to **is**.
4. Set **Choose** to **Least recently played**. Uncheck **Follow the repeat
   rules**, leave day and hour restrictions unset, and press **Add step**.
5. Add another step the same way, choosing **Practice other** instead.
6. Clear **Restart at these minutes past each hour**. Press **Save clockwheel**,
   then choose **Activate** from **Options for Practice alternating** in its saved row.
7. In **Settings**, **Song picking**, confirm **Pick mode** is **Clockwheel**.

The saved wheel should show two steps in order: Practice anchor, then Practice
other. Its **Active** column should say **Yes**. Each step's **Rules and hours**
column should say it ignores repeat rules. With a one-song pool, repeat rules can interfere with
the very repetition we are trying to observe; this small lesson deliberately
leaves them off.

If activation asks you to save, press **Save clockwheel** first. If a step names
the wrong category, open that step's edit control, correct **Music category**,
save the step, and save the wheel. Confirm the descriptions before resuming.

## Follow the rotation through a full wrap

1. In **Queue & Search**, use **Clear queue** and confirm if entries remain.
   Queued songs take priority and would obscure the automatic pattern.
2. In **Now Playing**, resume with **Play or Pause**. Let the song already
   loaded finish; it may have been selected under the previous wheel.
3. Observe at least four subsequent automatic songs. For each, check the
   title in **Now Playing**, then review **History**.
4. Compare each title with the anchor title. Expect anchor, other, anchor,
   other, allowing for an already prepared song at the changeover.

The wheel wraps after step 2. It controls the pool order; **Least recently
played** chooses a song inside each pool. The exact “other” titles depend on
your history. There is no fixed number of minutes per step.

If two other titles occur repeatedly without the anchor, pause. Check the
anchor category's count and unrestricted times, both saved step descriptions,
the active wheel, empty queue, and pick mode. An empty step can be skipped;
correct its category and save before resuming. Do not interpret a changing
title alone as proof that the wheel supplied it.

## Try it: change the balance

Predict what happens if the wheel contains anchor, other, other. Does doubling
the second step double its songs or its minutes?

Add a third step to Practice alternating by repeating the Practice other step's
settings. Set **Apply saved changes** to **Immediately, start at step 1**, then
press **Save clockwheel**. Observe a complete new cycle after any prepared song.

Expected result: one anchor selection followed by two selections from Practice
other. The pool gets two of every three steps, not a guaranteed two-thirds of
airtime: songs have different lengths. Explain your observed sequence using
the three saved step descriptions and History. To repeat the two-step example,
open the third step's **Options** menu and choose **Delete**, then save the wheel again.

## Stop and retain the example

Pause with **Play or Pause** and confirm the elapsed time stops. The two new
categories and Practice alternating remain saved. Keep them for experimentation;
no deletion is required. To restore the first lesson's rotation, select the
original `Practice` row in **Saved clockwheels**, open **Options for Practice**,
choose **Activate**, and leave
**Pick mode** set to **Clockwheel**. This restores the original wheel without
deleting your new work.

You can now shape a rotation using pool counts and ordered steps. Continue with
[Schedule and verify a short show](scheduled-show.md), or use
[Build your sound](../guides/build-your-sound.md) for repeat rules and larger pools.

For background, read [how selection works](../explanation/selection.md).
Before using a practice setup regularly, follow
[Prepare a station](../guides/prepare-station.md).

AutoDJ and this page were developed with AI assistance.
