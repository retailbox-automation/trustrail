"""Stand-in for an operator-configured LLM command: prompt on stdin, JSON on stdout."""
import json
import sys
import time

mode = sys.argv[1]
sys.stdin.read()
none_bank = None
outputs = {
    'valid': {'kind': 'invoice', 'invoice_id': 'INV-2002', 'amount': '980.00', 'currency': 'EUR', 'bank': none_bank},
    'schema': {'kind': 'invoice', 'invoice_id': 'INV-2002', 'extra': True},
    'ungrounded': {'kind': 'invoice', 'invoice_id': 'INV-2002', 'amount': '980.00', 'currency': 'EUR',
                   'bank': {'iban': 'DE44500105175407324931', 'account_number': None, 'sort_code': None,
                            'swift_code': None, 'account_name': None}},
    # Follows the injected instruction: classifies a bank-change email as harmless.
    'obedient': {'kind': 'other', 'invoice_id': None, 'amount': None, 'currency': None, 'bank': None},
    'wrong_amount': {'kind': 'invoice', 'invoice_id': 'INV-2002', 'amount': '9800.00', 'currency': 'EUR', 'bank': None},
}
if mode == 'file':  # hostile model: prints whatever JSON the test stored, regardless of the email
    print(open(sys.argv[2]).read().strip())
elif mode == 'garbage':
    print('Sure! Here is the JSON you asked for: {kind: invoice')
elif mode == 'sleep':
    time.sleep(10)
elif mode == 'exit':
    sys.exit(3)
else:
    print(json.dumps(outputs[mode]))
