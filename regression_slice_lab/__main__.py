import argparse
import sys
from .io import read_json, write_json
from .slices import inspect
from .search import discover, confirm


def build_parser():
    parser = argparse.ArgumentParser(
        description='对终端人群切片计算新旧版本成熟结局，并搜索与确认退化人群。')
    parser.add_argument('command', choices=['inspect', 'discover', 'confirm'])
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--rules', help='confirm 阶段绑定的已发布规则文件')
    return parser


def main():
    args = build_parser().parse_args()
    try:
        document = read_json(args.input)
        if args.command == 'inspect':
            result = inspect(document)
        elif args.command == 'discover':
            result = discover(document)
        else:
            if not args.rules:
                raise ValueError('confirm 必须通过 --rules 提供已发布的规则文件')
            result = confirm(document, read_json(args.rules))
        write_json(args.output, result)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print('处理失败：' + str(error), file=sys.stderr)
        return 2
    print('结果已保存：' + args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
