from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable


class Tool(ABC):
    """Base class for Minerva tools exposed through OpenAI tool calling."""

    name: str
    description: str
    risky: bool = False
    parameters: dict[str, Any]

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def validate(self, args: dict[str, Any]) -> None:
        """Runtime/business validation. JSON structure is handled by strict schemas."""
        return None

    @abstractmethod
    def run(self, **args: Any) -> Any:
        """Execute the tool."""
        raise NotImplementedError

    def openai_definition(self) -> dict[str, Any]:
        """Return the function-tool definition for OpenAI Chat Completions."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
                "strict": True,
            },
        }


class CustomerInfoTool(Tool):
    name = "customer_info"
    description = "Returns company_name, address, telephone and other company info."
    parameters = {
        "type": "object",
        "properties": {
            "customer_code": {"type": "string", "description": "Customer code."},
        },
        "required": ["customer_code"],
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> None:
        if not args["customer_code"].strip():
            raise ValueError("customer_code cannot be empty")

    def run(self, **args: Any) -> Any:
        return self.owner.mago_mcp.customer_info(**args)


class SearchPeopleTool(Tool):
    name = "search_people"
    description = "Returns customer_code or an empty result when not found."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "First name to search for."},
            "last_name": {"type": "string", "description": "Last name to search for."},
        },
        "required": ["name", "last_name"],
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> None:
        if not args["name"].strip() and not args["last_name"].strip():
            raise ValueError("name and last_name cannot both be empty")

    def run(self, **args: Any) -> Any:
        result = self.owner.mago_mcp.search_people(**args)
        return result


class ItemInfoTool(Tool):
    name = "item_info"
    description = "Returns item information such as description, price, etc."
    parameters = {
        "type": "object",
        "properties": {
            "item_code": {"type": "string", "description": "Item code."},
        },
        "required": ["item_code"],
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> None:
        if not args["item_code"].strip():
            raise ValueError("item_code cannot be empty")

    def run(self, **args: Any) -> Any:
        return self.owner.mago_mcp.item_info(**args)


class SearchItemTool(Tool):
    name = "search_item"
    description = "Returns item code or an empty result when not found. Wildcards are allowed."
    parameters = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Item description to search for. Wildcards are allowed.",
            },
        },
        "required": ["description"],
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> None:
        if not args["description"].strip():
            raise ValueError("description cannot be empty")

    def run(self, **args: Any) -> Any:
        return self.owner.mago_mcp.search_item(**args)


class StockLevelTool(Tool):
    name = "stock_level"
    description = (
        "Returns the current stock level of the item and the unit of measure "
        "it is expressed in, such as pieces or boxes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "item_code": {"type": "string", "description": "Item code."},
        },
        "required": ["item_code"],
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> None:
        if not args["item_code"].strip():
            raise ValueError("item_code cannot be empty")

    def run(self, **args: Any) -> Any:
        return self.owner.mago_mcp.item_stock_level(**args)


class DelegateTool(Tool):
    name = "delegate"
    description = "Ask a bounded read-only child agent to investigate."

    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Task for the child agent."
            },
            "max_steps": {
                "type": ["integer", "null"],
                "minimum": 1,
                "description": "Maximum number of investigation steps. Defaults to 3."
            },
        },
        "required": ["task", "max_steps"],
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> None:
        if not args["task"].strip():
            raise ValueError("task cannot be empty")

        if args["max_steps"] is not None and args["max_steps"] < 1:
            raise ValueError("max_steps must be at least 1")

    def run(self, **args: Any) -> Any:
        max_steps = args.get("max_steps") or 3

        return self.owner.mago_mcp.tool_delegate(
            task=args["task"],
            max_steps=max_steps
        )

def build_tools(owner: Any) -> dict[str, Tool]:
    """Build Minerva's tool registry, including DELEGATE only when allowed."""
    instances: list[Tool] = [
        CustomerInfoTool(owner),
        SearchPeopleTool(owner),
        ItemInfoTool(owner),
        SearchItemTool(owner),
        StockLevelTool(owner),
    ]

    if owner.depth < owner.max_depth:
        instances.append(DelegateTool(owner))

    return {tool.name: tool for tool in instances}
