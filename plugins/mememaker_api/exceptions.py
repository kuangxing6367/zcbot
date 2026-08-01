from argparse import ArgumentParser

class ArgParseError(Exception):
    """参数解析错误"""
    pass

class APIError(Exception):
    """API 请求错误"""
    pass

class NoExitArgumentParser(ArgumentParser):
    """一个在解析出错时会抛出 ArgParseError 异常而不是直接退出的解析器"""
    def error(self, message: str):
        raise ArgParseError(message)