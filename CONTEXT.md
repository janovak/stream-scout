# Domain Context

## Desired set

The ranked Twitch broadcasters that chat ingestion should currently cover.
The poller computes and publishes it; the reconciler reads it and drives live
subscriptions toward it. Each snapshot contains broadcaster logins in rank
order, their Twitch broadcaster ids, and a monotonic generation.

## Actual set

The live EventSub chat subscriptions the reconciler has adopted or created.
The reconciler compares this set with the desired set on each pass, creates
missing subscriptions, and drops subscriptions no longer desired.

## Reconcile pass

One comparison of the desired set with the actual set, including bounded
parallel subscription creates and drops. A generation change during a pass
causes the reconciler to refresh its desired view before creating more work.
