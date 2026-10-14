"""模型调用错误类型；可由各 provider 复用。"""
class AsrError(Exception):
    pass


class TtsError(Exception):
    pass
