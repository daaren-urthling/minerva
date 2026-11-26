import json
from typing import Any


class MagoMCP:

    def customer_info(self, customer_code: str) -> str:
        return {
                "customer_code": customer_code,
                "company_name": "ACME ltd",
                "tax_id_number": "02572020101",
                "telephone1": "040 5657746",
                "telephone2": "040 6657288",
                "telex": "123-acme-7756",
                "fax": "040 6577466",
                "internet": "www.acmeglobal.com",
                "e_mail": "sales@acmeglobal.com",
        }

    def search_people(
        self,
        name: str,
        last_name: str,
    ) -> str:
        return {
                "customer_code": "0042",
                "working_position": "owner",
            }

    def item_info(self, item_code: str) -> str:
        return {}

    def search_item(self, description: str) -> str:
        return {}

    def item_stock_level(self, item_code: str) -> str:
        return {}