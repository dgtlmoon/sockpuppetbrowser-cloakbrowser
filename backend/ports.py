import psutil

class PortSelector:
    def __init__(self):
        self.current_port = 9999

    def __iter__(self):
        return self

    def __next__(self):
        self.current_port += 1
        if self.current_port > 20000:
            self.current_port = 10000
        return self.current_port
