"""按 Git 命令的记录分隔符解析路径，不改写合法文件名字符。"""


def parse_git_path_records(payload: bytes, expected_count: int):
    # rev-parse 的每条记录以 LF（或 CRLF）结束。Unicode 的行分隔符和
    # 路径尾部空白属于名称，不能使用 str.splitlines() 或 str.strip()。
    if not payload.endswith(b'\n'):
        raise ValueError('Git 路径输出缺少记录结束符')
    records = payload[:-1].split(b'\n')
    if len(records) != expected_count:
        raise ValueError('Git 路径输出记录数不符')
    paths = [record.removesuffix(b'\r').decode('utf-8', 'surrogateescape')
             for record in records]
    if any('\x00' in path or '\r' in path for path in paths):
        raise ValueError('Git 路径输出包含不支持的控制字符')
    return paths
