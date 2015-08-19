# Copyright 2015 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import re
import string


_EOF = object()


class _NamedRegex(object):
    def __init__(self, name, regex_str):
        self.name = name
        self.regex = re.compile(regex_str, re.UNICODE)

    def match(self, value):
        return self.regex.match(value)


class ShorthandParseError(Exception):
    def __init__(self, value, expected, actual, index):
        self.value = value
        self.expected = expected
        self.actual = actual
        self.index = index
        msg = self._construct_msg()
        super(ShorthandParseError, self).__init__(msg)

    def _construct_msg(self):
        if '\n' in self.value:
            # If there's newlines in the expression, we want
            # to make sure we're only counting the spaces
            # from the last newline:
            # foo=bar,\n
            # bar==baz
            #     ^
            last_newline = self.value[:self.index].rindex('\n')
            num_spaces = self.index - last_newline - 1
        else:
            num_spaces = self.index
        msg = (
            "Expected: '%s', received: '%s' for input:\n"
            "%s\n"
            "%s\n"
        ) % (self.expected, self.actual, self.value,
             ' ' * num_spaces + '^')
        return msg


class ShorthandParser(object):
    """Parses shorthand syntax in the CLI.

    Note that this parser does not rely on any JSON models to control
    how to parse the shorthand syntax.

    """

    _SINGLE_QUOTED = _NamedRegex('singled quoted', r'\'(?:\\\\|\\\'|[^\'])*\'')
    _DOUBLE_QUOTED = _NamedRegex('double quoted', r'"(?:\\\\|\\"|[^"])*"')
    _FIRST_VALUE = _NamedRegex('first',
                               u'[\!\#-&\(-\+\--\<\>-Z\\\\-z\u007c-\uffff]'
                               u'[\!\#-&\(-\+\--\\\\\^-\|~-\uffff]*')
    _SECOND_VALUE = _NamedRegex('second',
                                u'[\!\#-&\(-\+\--\<\>-Z\\\\-z\u007c-\uffff]'
                                u'[\!\#-&\(-\+\--\<\>-\uffff]*')

    def __init__(self):
        self._tokens = []

    def parse(self, value):
        """Parse shorthand syntax.

        For example::

            parser = ShorthandParser()
            parser.parse('a=b')  # {'a': 'b'}
            parser.parse('a=b,c')  # {'a': ['b', 'c']}

        :tpye value: str
        :param value: Any value that needs to be parsed.

        :return: Parsed value, which will be a dictionary.
        """
        self._input_value = value
        self._index = 0
        return self._parameter()

    def _parameter(self):
        # parameter = keyval *("," keyval)
        params = {}
        params.update(self._keyval())
        while self._index < len(self._input_value):
            self._expect(',', consume_whitespace=True)
            params.update(self._keyval())
        return params

    def _keyval(self):
        # keyval = key "=" [values]
        key = self._key()
        self._expect('=', consume_whitespace=True)
        values = self._values()
        return {key: values}

    def _key(self):
        # key = 1*(alpha / %x30-39)  ; [a-zA-Z0-9]
        valid_chars = string.ascii_letters + string.digits
        start = self._index
        while not self._at_eof():
            if self._current() not in valid_chars:
                break
            self._index += 1
        return self._input_value[start:self._index]

    def _values(self):
        # values = csv-list / explicit-list / hash-literal
        if self._at_eof():
            return ''
        elif self._current() == '[':
            return self._explicit_list()
        elif self._current() == '{':
            return self._hash_literal()
        else:
            return self._csv_value()

    def _csv_value(self):
        # Supports either:
        # foo=bar     -> 'bar'
        #     ^
        # foo=bar,baz -> ['bar', 'baz']
        #     ^
        first_value = self._first_value()
        self._consume_whitespace()
        if self._at_eof() or self._input_value[self._index] != ',':
            return first_value
        self._expect(',', consume_whitespace=True)
        csv_list = [first_value]
        # Try to parse remaining list values.
        # It's possible we don't parse anything:
        # a=b,c=d
        #     ^-here
        # In the case above, we'll hit the ShorthandParser,
        # backtrack to the comma, and return a single scalar
        # value 'b'.
        while True:
            try:
                current = self._second_value()
                if current is None:
                    break
                self._consume_whitespace()
                if self._at_eof():
                    csv_list.append(current)
                    break
                self._expect(',', consume_whitespace=True)
                csv_list.append(current)
            except ShorthandParseError:
                # Backtrack to the previous comma.
                # This can happen when we reach this case:
                # foo=a,b,c=d,e=f
                #     ^-start
                # foo=a,b,c=d,e=f
                #          ^-error, "expected ',' received '='
                # foo=a,b,c=d,e=f
                #        ^-backtrack to here.
                self._backtrack_to(',')
                break
        if len(csv_list) == 1:
            # Then this was a foo=bar case, so we expect
            # this to parse to a scalar value 'bar', i.e
            # {"foo": "bar"} instead of {"bar": ["bar"]}
            return first_value
        return csv_list

    def _value(self):
        result = self._FIRST_VALUE.match(self._input_value[self._index:])
        if result is not None:
            return self._consume_matched_regex(result)
        return ''

    def _explicit_list(self):
        # explicit-list = "[" [value *(",' value)] "]"
        self._expect('[', consume_whitespace=True)
        values = []
        while self._current() != ']':
            val = self._explicit_values()
            values.append(val)
            self._consume_whitespace()
            if self._current() != ']':
                self._expect(',')
                self._consume_whitespace()
        self._expect(']')
        return values

    def _explicit_values(self):
        # values = csv-list / explicit-list / hash-literal
        if self._at_eof():
            return ''
        elif self._current() == '[':
            return self._explicit_list()
        elif self._current() == '{':
            return self._hash_literal()
        else:
            return self._first_value()

    def _hash_literal(self):
        self._expect('{', consume_whitespace=True)
        keyvals = {}
        while self._current() != '}':
            key = self._key()
            self._expect('=', consume_whitespace=True)
            v = self._explicit_values()
            self._consume_whitespace()
            if self._current() != '}':
                self._expect(',')
                self._consume_whitespace()
            keyvals[key] = v
        self._expect('}')
        return keyvals

    def _first_value(self):
        # first-value = value / single-quoted-val / double-quoted-val
        if self._current() == "'":
            return self._single_quoted_value()
        elif self._current() == '"':
            return self._double_quoted_value()
        return self._value()

    def _single_quoted_value(self):
        # single-quoted-value = %x27 *(val-escaped-single) %x27
        # val-escaped-single  = %x20-26 / %x28-7F / escaped-escape /
        #                       (escape single-quote)
        return self._consume_quoted(self._SINGLE_QUOTED, escaped_char="'")

    def _consume_quoted(self, regex, escaped_char=None):
        value = self._must_consume_regex(regex)[1:-1]
        if escaped_char is not None:
            value = value.replace("\\%s" % escaped_char, escaped_char)
            value = value.replace("\\\\", "\\")
        return value

    def _double_quoted_value(self):
        return self._consume_quoted(self._DOUBLE_QUOTED, escaped_char='"')

    def _second_value(self):
        if self._current() == "'":
            return self._single_quoted_value()
        elif self._current() == '"':
            return self._double_quoted_value()
        else:
            return self._must_consume_regex(self._SECOND_VALUE)

    def _expect(self, char, consume_whitespace=False):
        if consume_whitespace:
            self._consume_whitespace()
        if self._index >= len(self._input_value):
            raise ShorthandParseError(self._input_value, char,
                                      'EOF', self._index)
        actual = self._input_value[self._index]
        if actual != char:
            raise ShorthandParseError(self._input_value, char,
                                      actual, self._index)
        self._index += 1
        if consume_whitespace:
            self._consume_whitespace()

    def _must_consume_regex(self, regex):
        result = regex.match(self._input_value[self._index:])
        if result is not None:
            return self._consume_matched_regex(result)
        raise ShorthandParseError(self._input_value, '<%s>' % regex.name,
                                  '<none>', self._index)

    def _consume_matched_regex(self, result):
        start, end = result.span()
        v = self._input_value[self._index+start:self._index+end]
        self._index += (end - start)
        return v

    def _current(self):
        # If the index is at the end of the input value,
        # then _EOF will be returned.
        if self._index < len(self._input_value):
            return self._input_value[self._index]
        return _EOF

    def _at_eof(self):
        return self._index >= len(self._input_value)

    def _backtrack_to(self, char):
        while self._index >= 0 and self._input_value[self._index] != char:
            self._index -= 1

    def _consume_whitespace(self):
        while self._current() != _EOF and self._current() in string.whitespace:
            self._index += 1
