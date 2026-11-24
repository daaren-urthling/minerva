**Valid response examples**
<service>{"name":"CUSTOMER_INFO","args":{"customer_code":"0042"}}</service>
<service>{"name":"SEARCH_PEOPLE","args":{"name":"John","last_name": "Doe"} }</service>
<service>{"name":"ITEM_INFO","args":{"item_code":"ITM0099"}}</service>
<service>{"name":"SEARCH_ITEM","args":{"description": "Rocky Mountains Bike"} }</service>
<service>{"name":"STOCK_LEVEL","args":{"item_code":"ITM0099"} }</service>
<service>{"name":"DELEGATE","args":{"task":"find stock levels of ITM0099","max_steps":3}}</tool>',
<final>Done.</final>

**Example of a multi-step task:**

    User:
    What is John Smith's telephone number?

    Assistant:
    <service>{"name":"SEARCH_PEOPLE","args":{"name":"John","last_name":"Smith"}}</service>

    Runtime:
    {"customer_code":"C00042"}

    Assistant:
    <service>{"name":"CUSTOMER_INFO","args":{"customer_code":"C00042"}}</service>

    Runtime:
    {"company_name":"ACME","telephone":"+39 010 1234567"}

    Assistant:
    <final>John Smith's company telephone number is +39 010 1234567.</final>