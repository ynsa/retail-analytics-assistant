You use the assistant as **{{ name }}**, {{ role_text }}.

**Your brands:** {{ brands_text }}.

**Data you can check** (sales from {{ first_date }} to today, {{ today }}):

{{ overview }}
{% if manager %}

Customer figures cover only the customers who bought your brands.
{% endif %}

**Example questions:**

{% for question in examples %}
{{ loop.index }}. {{ question }}
{% endfor %}
