<!-- canary-7f3a9c -->
# Task: correct the figures of some report sentences

Code checked each number in the report text. The numbers in the sentences of `<sentences>` do not come from the query results in the `<data>` blocks.

Rewrite each sentence:

1. Keep the meaning and the length.
2. Use only figures that a data block shows, copied exactly.
3. For a derived figure, write a calculation placeholder. {% raw %}Format: `{{calc:OPERATION(REF, REF)}}` with the operations `diff`, `pct_change`, `ratio`, `share`, `sum` and `avg`. A REF is `r1.column@key`.{% endraw %} Only REFs are inputs.
4. If no data block supports the figure, remove the figure from the sentence.

Return `sentences`: one text for each sentence, in the same order.
