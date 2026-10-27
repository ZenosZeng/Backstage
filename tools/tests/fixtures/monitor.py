"""Side-effect-free monitor used by public lifecycle integration tests."""

class Monitor:
    name = "test monitor"
    alert_interval = 60

    def __init__(self, text):
        self.text = text

    def check_alerts(self):
        return []

    def status(self):
        return self.text

    def active(self):
        return False


def create_monitor(config, config_path, directory, notifier):
    return Monitor(config.get("status_text", "fixture status"))
