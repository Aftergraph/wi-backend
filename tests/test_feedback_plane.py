from aftergraph_work_intelligence.feedback import (
    cluster_feedback,
    create_feedback_event,
)


def test_feedback_is_proposal_only():
    event = create_feedback_event(
        event_id="fb_1",
        observed_at="2026-10-01T00:00:00Z",
        source="homeos",
        kind="idea",
        summary="Add visual editing",
        source_ref="feedback://1",
        labels=("ui",),
    )
    contract = event.to_contract()
    assert contract["schema"] == "aftergraph.feedback-event/v1"
    assert contract["authority"] == {
        "execution_authority": "none",
        "promotion_required": True,
    }


def test_clusters_duplicate_feedback_without_promoting():
    events = [
        create_feedback_event(
            event_id="fb_1",
            observed_at="2026-10-01T00:00:00Z",
            source="homeos",
            kind="bug",
            summary="Visual editor loses source anchor",
            source_ref="feedback://1",
            labels=("visual-edit",),
        ),
        create_feedback_event(
            event_id="fb_2",
            observed_at="2026-10-01T00:01:00Z",
            source="web",
            kind="bug",
            summary="Visual editor loses source anchor",
            source_ref="feedback://2",
            labels=("runtime",),
        ),
    ]
    proposals = cluster_feedback(events)
    assert len(proposals) == 1
    assert proposals[0].event_refs == ("fb_1", "fb_2")
    assert proposals[0].execution_authority == "none"
    assert proposals[0].promotion_required is True
