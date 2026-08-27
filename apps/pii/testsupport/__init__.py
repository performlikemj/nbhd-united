"""Test support for PII detector fixtures."""


def neural_ran(return_value):
    """Return a detector stub that records neural availability before returning."""

    def side_effect(*args, **kwargs):
        from apps.pii.redactor import _neural_detector_outcome

        _neural_detector_outcome.available = True
        if callable(return_value):
            return return_value(*args, **kwargs)
        return return_value

    return side_effect
