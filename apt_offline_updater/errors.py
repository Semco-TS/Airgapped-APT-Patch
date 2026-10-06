class Cancelled(Exception):
    pass


class WorkflowError(Exception):
    pass


class EmptySig(ValueError):
    pass