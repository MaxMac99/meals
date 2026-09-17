"""The assistant's tools: the MCP server's task semantics, executed directly
against the router functions.

Three sources of truth, deliberately not duplicated:

- **Names, descriptions and argument schemas** come from the MCP server at
  runtime (``meals_mcp.server.mcp.list_tools()`` — see ``tool_definitions``).
  The built-in assistant and a remote MCP client are therefore the *same*
  agent with the same vocabulary, and ``skill/SKILL.md`` — written for that
  agent — reads as instructions for this one without a fork of the rules.
- **Behaviour** comes from the existing routers (``routers/*.py``), called
  in-process with the chat request's own session and user. Limits, list
  re-sync, validation and household scoping are the routers' to own; a tool
  here only resolves human names ("the cottage pie") into the objects the
  router functions want, which is agent convenience, not domain logic.
- **What needs a household's yes** is declared here per tool
  (``destructive`` / ``destructive_for``), in the vocabulary SKILL.md's
  "when to ask vs act" section already uses.

Handlers raise whatever their router raises (``HTTPException`` carrying the
same 4xx sentences); the runtime turns one failed tool into a result the
model can read and react to, never into a crashed turn.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.routers import freezer as freezer_router
from app.routers import ingredients as ingredients_router
from app.routers import limits as limits_router
from app.routers import meals as meals_router
from app.routers import plans as plans_router
from app.routers import recipes as recipes_router
from app.routers import shopping as shopping_router
from app.routers import supermarkets as supermarkets_router
from app.routers.ingredients import IngredientCreate
from app.schemas.catalog import IngestIn, IngredientUpdate, MergeIn, RecipeCreate, RecipeUpdate, ReparseIn
from app.schemas.common import IngredientLineIn
from app.schemas.freezer import FreezerAddIn, FreezerTakeIn
from app.schemas.planning import (
    AddMealIn,
    MealCreate,
    MealRecipeIn,
    MealUpdate,
    PlanCreate,
)
from app.schemas.shopping import (
    AdhocItemIn,
    ListItemUpdate,
    SupermarketCreate,
    SupermarketUpdate,
)

#: The handler for one tool. ``args`` is the model's argument object after
#: schema validation; the return value is the tool result the model sees
#: (a response model, a plain dict, or None for a bare delete).
Handler = Callable[[User, AsyncSession, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Handler
    destructive: bool = False
    #: For tools that are only sometimes destructive: take_from_freezer with
    #: all_of_it, reparse_recipe with force. True means the household must
    #: confirm this particular call.
    destructive_for: Callable[[dict[str, Any]], bool] | None = None
    #: What a single-object result names, for the app's reference chips.
    ref_type: str | None = None

    def requires_confirmation(self, arguments: dict[str, Any]) -> bool:
        if self.destructive_for is not None:
            return self.destructive_for(arguments)
        return self.destructive


# ---------------------------------------------------------------- helpers
# Name resolution — the same conveniences the MCP server performs over HTTP
# (meals_mcp.server._find_meal and friends), here against router calls.


async def _all_meals(user: User, db: AsyncSession) -> list[Any]:
    # Called directly, Query defaults are Query objects rather than their
    # values — every optional parameter is passed explicitly, everywhere.
    return await meals_router.list_meals(user=user, db=db, search=None, slot=None)


async def _find_meal(user: User, db: AsyncSession, name: str) -> Any:
    """A meal worth editing isn't necessarily on the current plan."""
    wanted = name.lower().strip()
    meals = await _all_meals(user, db)
    exact = [meal for meal in meals if meal.name.lower() == wanted]
    partial = [meal for meal in meals if wanted in meal.name.lower()]
    match = exact or partial
    if not match:
        known = ", ".join(meal.name for meal in meals) or "(no meals yet)"
        raise LookupError(f"No meal matching '{name}'. Meals: {known}")
    return match[0]


async def _resolve_recipes(user: User, db: AsyncSession, terms: list[str]) -> list[tuple[uuid.UUID, Any]]:
    """Accept recipe ids or titles — 'add garlic bread' shouldn't need an id
    looked up first. Returns (id, RecipeSummary) pairs."""
    if not terms:
        return []
    library = await recipes_router.list_recipes(
        user=user, db=db, search=None, tag=None, max_total_minutes=None, sort="title"
    )
    resolved = []
    for term in terms:
        wanted = str(term).lower().strip()
        match = [r for r in library if str(r.id) == wanted]
        match = match or [r for r in library if r.title.lower() == wanted]
        match = match or [r for r in library if wanted in r.title.lower()]
        if not match:
            known = ", ".join(r.title for r in library) or "(library is empty)"
            raise LookupError(f"No recipe matching '{term}'. Library: {known}")
        resolved.append((match[0].id, match[0]))
    return resolved


async def _recipe_amounts(
    user: User,
    db: AsyncSession,
    recipe_terms: list[str] | None,
    scales: dict[str, float] | None,
    servings: dict[str, int] | None,
) -> list[MealRecipeIn]:
    """MCP's recipe_scales / recipe_servings choreography: named-or-id'd
    recipes, one multiplier or portion count each, never both."""
    scales = scales or {}
    servings = servings or {}
    amounts: list[MealRecipeIn] = []
    for recipe_id, _ in await _resolve_recipes(user, db, list(recipe_terms or [])):
        key = str(recipe_id)
        if key in servings:
            amounts.append(MealRecipeIn(recipe_id=recipe_id, servings=servings[key]))
        else:
            amounts.append(MealRecipeIn(recipe_id=recipe_id, scale=scales.get(key, 1.0)))
    return amounts


async def _find_item(user: User, db: AsyncSession, name: str) -> Any:
    """A list line by name, staples and already-have lines included."""
    data = await shopping_router.get_shopping_list(user=user, db=db, include_staples=True, include_excluded=True)
    wanted = name.lower().strip()
    exact = [item for item in data.items if item.name == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        options = "; ".join(f"{item.name} ({item.display or 'no qty'})" for item in exact)
        raise LookupError(f"'{name}' matches several lines with different units: {options}. Be more specific.")
    partial = [item for item in data.items if wanted in item.name]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(sorted({item.name for item in data.items})) or "(list is empty)"
    raise LookupError(f"No list item matching '{name}'. Items on the list: {names}")


async def _find_ingredient(user: User, db: AsyncSession, name: str) -> Any:
    # The ?name= lookup folds the same way a write does, so "mint leaves"
    # resolves to the row filed under "mint" rather than a near miss.
    found = await ingredients_router.list_ingredients(
        user=user, db=db, name=name, search=None, staples_only=False, value_tier=None, sort="name"
    )
    if found:
        return found[0]
    similar = await ingredients_router.list_ingredients(
        user=user, db=db, search=name, name=None, staples_only=False, value_tier=None, sort="name"
    )
    exact = [i for i in similar if i.name == name.lower().strip()]
    if not exact:
        names = ", ".join(i.name for i in similar) or "none like that"
        raise LookupError(f"No ingredient '{name}' (similar: {names}).")
    return exact[0]


async def _resolve_freezer_subject(user: User, db: AsyncSession, name: str) -> dict[str, Any]:
    """Meals first, then recipes; exact beats a single partial. Anything else
    is free text — right for food that never came through the plan."""
    wanted = name.lower().strip()
    meals = await _all_meals(user, db)
    exact_meal = [m for m in meals if m.name.lower() == wanted]
    if exact_meal:
        return {"meal_id": exact_meal[0].id}
    recipes = await recipes_router.list_recipes(
        user=user, db=db, search=None, tag=None, max_total_minutes=None, sort="title"
    )
    exact_recipe = [r for r in recipes if r.title.lower() == wanted]
    if exact_recipe:
        return {"recipe_id": exact_recipe[0].id}
    partial_meals = [m for m in meals if wanted in m.name.lower()]
    partial_recipes = [r for r in recipes if wanted in r.title.lower()]
    if len(partial_meals) + len(partial_recipes) == 1:
        if partial_meals:
            return {"meal_id": partial_meals[0].id}
        return {"recipe_id": partial_recipes[0].id}
    return {"label": name.strip()}


# ---------------------------------------------------------------- recipes


async def tool_ingest_recipe(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await recipes_router.ingest_recipe_url(payload=IngestIn(url=args["url"]), user=user, db=db)


async def tool_submit_recipe(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    payload = RecipeCreate(
        title=args["title"],
        ingredients=args.get("ingredients") or [],
        source_url=args.get("source_url"),
        servings=args.get("servings"),
        prep_minutes=args.get("prep_minutes"),
        cook_minutes=args.get("cook_minutes"),
        instructions=args.get("instructions"),
        tags=args.get("tags") or [],
        parse_source="ai" if args.get("source_url") else "manual",
    )
    return await recipes_router.create_recipe(payload=payload, user=user, db=db, response=Response())


async def tool_reparse_recipe(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    recipe_id, _ = (await _resolve_recipes(user, db, [args["recipe"]]))[0]
    return await recipes_router.reparse_recipe(
        recipe_id=recipe_id, payload=ReparseIn(force=bool(args.get("force"))), user=user, db=db
    )


async def tool_delete_recipe(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    recipe_id, recipe = (await _resolve_recipes(user, db, [args["title"]]))[0]
    await recipes_router.delete_recipe(recipe_id=recipe_id, user=user, db=db)
    return {"deleted": recipe.title}


async def tool_update_recipe(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    """The PATCH-only shapes travel: whatever the model didn't name stays
    untouched, so a rename can't blank the recipe."""
    recipe_id, _ = (await _resolve_recipes(user, db, [args["recipe"]]))[0]
    fields: dict[str, Any] = {
        key: args[key]
        for key in ("title", "servings", "prep_minutes", "cook_minutes", "instructions", "tags", "ingredients")
        if args.get(key) is not None
    }
    if not fields:
        return "Nothing to change. Pass title, servings, prep_minutes, cook_minutes, instructions, tags or ingredients."
    return await recipes_router.update_recipe(recipe_id=recipe_id, payload=RecipeUpdate(**fields), user=user, db=db)


async def tool_list_recipes(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await recipes_router.list_recipes(
        user=user,
        db=db,
        search=args.get("search"),
        tag=args.get("tag"),
        max_total_minutes=args.get("max_total_minutes"),
        sort=args.get("sort") or "title",
    )


# ------------------------------------------------------------- meals & plan


async def tool_create_meal(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    payload = MealCreate(
        name=args["name"],
        slot=args.get("slot") or "dinner",
        recipes=await _recipe_amounts(
            user, db, args.get("recipe_ids"), args.get("recipe_scales"), args.get("recipe_servings")
        ),
        loose_ingredients=args.get("loose_ingredients") or [],
    )
    return await meals_router.create_meal(payload=payload, user=user, db=db)


async def tool_update_meal(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    """The MCP update_meal choreography, against router functions: recipes
    can be named or id'd, scales carry through untouched, and loose lines
    are replaced wholesale (the API's contract for PATCH)."""
    meal = await _find_meal(user, db, args["meal_name"])
    fields: dict[str, Any] = {}
    if args.get("new_name"):
        fields["name"] = args["new_name"]
    if args.get("slot"):
        fields["slot"] = args["slot"]

    adds = args.get("add_recipes") or []
    drops = args.get("remove_recipes") or []
    scale_asked = {key.lower().strip(): value for key, value in (args.get("scale_recipes") or {}).items()}
    servings_asked = {key.lower().strip(): value for key, value in (args.get("recipe_servings") or {}).items()}

    if adds or drops or scale_asked or servings_asked:
        recipes = list(meal.recipes)
        kept = [MealRecipeIn(recipe_id=recipe.id, scale=recipe.scale) for recipe in recipes]
        wanted_ids = {recipe.id for recipe in recipes}
        for recipe_id, _ in await _resolve_recipes(user, db, adds):
            if recipe_id not in wanted_ids:
                kept.append(MealRecipeIn(recipe_id=recipe_id))
                wanted_ids.add(recipe_id)
        for term in drops:
            drop_id, recipe = (await _resolve_recipes(user, db, [term]))[0]
            if drop_id not in wanted_ids:
                raise LookupError(f"'{recipe.title}' isn't in '{meal.name}'.")
            wanted_ids.discard(drop_id)
        # Resolve scales/portion counts by the term the model used, then set
        # them on the kept line; portions win where both were asked for, and
        # the API refuses to take both for one recipe anyway.
        for term in dict.fromkeys(list(servings_asked) + list(scale_asked)):
            recipe_id, recipe = (await _resolve_recipes(user, db, [term]))[0]
            if recipe_id not in wanted_ids:
                raise LookupError(f"'{recipe.title}' isn't in '{meal.name}' — add it first, then scale it.")
            line = next(line for line in kept if line.recipe_id == recipe_id)
            if term in servings_asked:
                line.servings = int(servings_asked[term])
            else:
                line.scale = float(scale_asked[term])
        kept = [line for line in kept if line.recipe_id in wanted_ids]
        fields["recipes"] = kept

    if args.get("add_loose_ingredients") is not None or args.get("remove_loose_ingredients") is not None:
        dropped = {name.lower().strip() for name in (args.get("remove_loose_ingredients") or [])}
        lines = [
            {"name": line.name, "quantity": line.quantity, "unit": line.unit}
            for line in meal.loose_ingredients
            if line.name.lower().strip() not in dropped
        ]
        lines.extend(args.get("add_loose_ingredients") or [])
        fields["loose_ingredients"] = [IngredientLineIn(**line) for line in lines]

    if not fields:
        return (
            f"Nothing to change on '{meal.name}'. Pass new_name, slot, add_recipes, remove_recipes, "
            "add_loose_ingredients, or remove_loose_ingredients."
        )
    return await meals_router.update_meal(meal_id=meal.id, payload=MealUpdate(**fields), user=user, db=db)


async def tool_delete_meal(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    meal = await _find_meal(user, db, args["meal_name"])
    await meals_router.delete_meal(meal_id=meal.id, user=user, db=db)
    return {"deleted": meal.name}


async def tool_get_plan(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await plans_router.current_plan(user=user, db=db)


async def tool_create_plan(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await plans_router.create_plan(
        payload=PlanCreate(label=args["label"], copy_from_plan_id=args.get("copy_from_plan_id")), user=user, db=db
    )


async def tool_add_meal_to_plan(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    meal = await _find_meal(user, db, args["meal_id"])
    plan = await plans_router.current_plan(user=user, db=db)
    return await plans_router.add_meal(plan_id=plan.id, payload=AddMealIn(meal_id=meal.id), user=user, db=db)


async def _plan_meal_by_name(plan: Any, meal_name: str) -> Any:
    wanted = meal_name.lower().strip()
    matches = [entry for entry in plan.meals if entry.meal.name.lower() == wanted]
    if not matches:
        names = ", ".join(entry.meal.name for entry in plan.meals) or "(plan is empty)"
        raise LookupError(f"No meal called '{meal_name}' in the current plan. Meals in plan: {names}")
    return matches[0]


async def tool_remove_meal_from_plan(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    plan = await plans_router.current_plan(user=user, db=db)
    entry = await _plan_meal_by_name(plan, args["meal_name"])
    return await plans_router.remove_meal(plan_meal_id=entry.id, plan_id=plan.id, user=user, db=db)


async def tool_mark_meal_cooked(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    plan = await plans_router.current_plan(user=user, db=db)
    entry = await _plan_meal_by_name(plan, args["meal_name"])
    return await plans_router.mark_cooked(plan_meal_id=entry.id, plan_id=plan.id, user=user, db=db)


async def tool_undo_meal_cooked(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    plan = await plans_router.current_plan(user=user, db=db)
    entry = await _plan_meal_by_name(plan, args["meal_name"])
    return await plans_router.undo_mark_cooked(plan_meal_id=entry.id, plan_id=plan.id, user=user, db=db)


# ------------------------------------------------------------ shopping list


async def tool_get_shopping_list(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await shopping_router.get_shopping_list(
        user=user, db=db, include_staples=bool(args.get("include_staples")), include_excluded=False
    )


async def tool_add_to_list(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    payload = AdhocItemIn(id=uuid.uuid4(), name=args["name"], quantity=args.get("quantity"), unit=args.get("unit"))
    return await shopping_router.add_item(payload=payload, user=user, db=db, response=Response())


async def tool_check_off(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    item = await _find_item(user, db, args["item_name"])
    return await shopping_router.update_item(item_id=item.id, payload=ListItemUpdate(checked=True), user=user, db=db)


async def tool_mark_already_have(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    item = await _find_item(user, db, args["item_name"])
    return await shopping_router.update_item(item_id=item.id, payload=ListItemUpdate(excluded=True), user=user, db=db)


async def tool_need_staple(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    item = await _find_item(user, db, args["item_name"])
    if not item.is_staple:
        raise LookupError(f"{item.name} isn't a staple — it's on the main list already.")
    return await shopping_router.update_item(
        item_id=item.id, payload=ListItemUpdate(staple_needed=bool(args.get("needed", True))), user=user, db=db
    )


async def tool_finish_shop(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await shopping_router.archive_shopping_list(user=user, db=db)


# ------------------------------------------------------------------ freezer


async def tool_get_freezer(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await freezer_router.get_freezer(user=user, db=db)


async def tool_add_to_freezer(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    name = str(args.get("name", "")).strip()
    if not name:
        raise LookupError("Say what went in — a meal, a recipe, or a name for it.")
    if int(args.get("portions", 1)) < 1:
        raise LookupError("Portions has to be at least 1 — a batch of nothing is not in the freezer.")
    if args.get("as_text"):
        subject: dict[str, Any] = {"label": name}
    else:
        subject = await _resolve_freezer_subject(user, db, name)
    payload = FreezerAddIn(
        **subject,
        portions=int(args.get("portions", 1)),
        note=args.get("note"),
        frozen_on=args.get("frozen_on"),
    )
    return await freezer_router.add_to_freezer(payload=payload, user=user, db=db)


async def tool_take_from_freezer(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    """Oldest batch first, spilling into the next; ``all_of_it`` bins every
    batch of that name (the destructive case)."""
    wanted = str(args.get("name", "")).lower().strip()
    if not wanted:
        raise LookupError("Say which batch — get_freezer() lists what is in there.")
    stock = await freezer_router.get_freezer(user=user, db=db)
    matches = [item for item in stock.items if item.label.lower() == wanted]
    matches = matches or [item for item in stock.items if wanted in item.label.lower()]
    if not matches:
        labels = sorted({item.label for item in stock.items}) or ["(the freezer is empty)"]
        raise LookupError(f"Nothing called '{args.get('name')}' in the freezer. In there: {', '.join(labels)}")
    label, held = matches[0].label, sum(item.portions for item in matches)
    if args.get("all_of_it"):
        for item in matches:
            await freezer_router.remove_from_freezer(item_id=item.id, user=user, db=db)
        return {"taken": held, "label": label, "left": 0}
    asked = int(args.get("portions", 1))
    to_take = asked
    for item in matches:  # oldest first, as the API lists them
        if to_take <= 0:
            break
        take = min(item.portions, to_take)
        await freezer_router.take_from_freezer(item_id=item.id, payload=FreezerTakeIn(portions=take), user=user, db=db)
        to_take -= take
    taken = asked - to_take
    return {"taken": taken, "label": label, "left": held - taken}


# -------------------------------------------------------------- ingredients


async def tool_set_ingredient_aisle(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    ingredient = await _find_ingredient(user, db, args["ingredient_name"])
    payload = IngredientUpdate(aisle=args["aisle_emoji"])
    if args.get("is_staple") is not None:
        payload.is_staple = bool(args["is_staple"])
    return await ingredients_router.update_ingredient(ingredient_id=ingredient.id, payload=payload, user=user, db=db)


async def tool_set_ingredient_value(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    ingredient = await _find_ingredient(user, db, args["ingredient_name"])
    payload = IngredientUpdate(value_tier=args["tier"])
    if "why" in args:
        payload.value_note = args["why"]  # "" clears the stale reason with the tier
    elif args["tier"] == "any":
        payload.value_note = ""
    return await ingredients_router.update_ingredient(ingredient_id=ingredient.id, payload=payload, user=user, db=db)


async def tool_list_ingredients_by_value(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await ingredients_router.list_ingredients(
        user=user,
        db=db,
        value_tier=args.get("tier") or "premium",
        search=None,
        name=None,
        staples_only=False,
        sort="name",
    )


async def tool_find_duplicate_ingredients(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await ingredients_router.list_duplicate_ingredients(user=user, db=db)


async def tool_merge_ingredients(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    keeper = await ingredients_router.create_ingredient(payload=IngredientCreate(name=args["keep"]), user=user, db=db)
    duplicate_ids: list[uuid.UUID] = []
    for name in args.get("duplicates") or []:
        found = await _find_ingredient(user, db, name)
        if found.id != keeper.id:
            duplicate_ids.append(found.id)
    if not duplicate_ids:
        return f"Nothing to merge — '{keeper.name}' is already the only name for it."
    return await ingredients_router.merge_into_ingredient(
        ingredient_id=keeper.id, payload=MergeIn(duplicate_ids=duplicate_ids), user=user, db=db
    )


async def tool_delete_ingredient(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    ingredient = await _find_ingredient(user, db, args["name"])
    await ingredients_router.delete_ingredient(ingredient_id=ingredient.id, user=user, db=db)
    return {"deleted": ingredient.name}


# -------------------------------------------------------------- supermarkets


async def tool_list_supermarkets(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await supermarkets_router.list_supermarkets(user=user, db=db)


async def tool_switch_supermarket(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    wanted = str(args.get("name", "")).lower().strip()
    markets = await supermarkets_router.list_supermarkets(user=user, db=db)
    if wanted in ("default", "none", ""):
        active = [market for market in markets if market.is_active]
        if not active:
            return "Already on the default store-walking order."
        return await supermarkets_router.update_supermarket(
            supermarket_id=active[0].id, payload=SupermarketUpdate(is_active=False), user=user, db=db
        )
    matches = [market for market in markets if market.name.lower() == wanted]
    if not matches:
        names = ", ".join(market.name for market in markets)
        if not names:
            raise LookupError(f"No supermarkets saved yet — save_supermarket('{args.get('name')}', [...]) creates one.")
        raise LookupError(f"No supermarket called '{args.get('name')}'. Saved: {names}. Or save_supermarket to add it.")
    return await supermarkets_router.update_supermarket(
        supermarket_id=matches[0].id, payload=SupermarketUpdate(is_active=True), user=user, db=db
    )


async def tool_save_supermarket(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    name = str(args["name"]).strip()
    markets = await supermarkets_router.list_supermarkets(user=user, db=db)
    existing = [market for market in markets if market.name.lower() == name.lower()]
    make_active = bool(args.get("make_active", True))
    if existing:
        payload = SupermarketUpdate(aisle_order=args.get("aisle_order"))
        if make_active:
            payload.is_active = True
        return await supermarkets_router.update_supermarket(
            supermarket_id=existing[0].id, payload=payload, user=user, db=db
        )
    return await supermarkets_router.create_supermarket(
        payload=SupermarketCreate(name=name, aisle_order=args.get("aisle_order"), is_active=make_active),
        user=user,
        db=db,
    )


# --------------------------------------------------------------------- meta


async def tool_check_limits(user: User, db: AsyncSession, args: dict[str, Any]) -> Any:
    return await limits_router.get_limits(user=user, db=db)


#: The assistant's tool table. Names must be a subset of the MCP server's
#: tool names — tool_definitions() builds the wire definitions from the MCP
#: server, so a name here without an MCP tool there would never reach the
#: model (and a test fails until one of the two is fixed).
TOOLS: dict[str, ToolSpec] = {
    "ingest_recipe": ToolSpec(name="ingest_recipe", handler=tool_ingest_recipe, ref_type="recipe"),
    "reparse_recipe": ToolSpec(
        name="reparse_recipe",
        handler=tool_reparse_recipe,
        destructive_for=lambda args: bool(args.get("force")),
        ref_type="recipe",
    ),
    "submit_recipe": ToolSpec(name="submit_recipe", handler=tool_submit_recipe, ref_type="recipe"),
    "update_recipe": ToolSpec(name="update_recipe", handler=tool_update_recipe, ref_type="recipe"),
    "list_recipes": ToolSpec(name="list_recipes", handler=tool_list_recipes),
    "delete_recipe": ToolSpec(name="delete_recipe", handler=tool_delete_recipe, destructive=True),
    "create_meal": ToolSpec(name="create_meal", handler=tool_create_meal, ref_type="meal"),
    "update_meal": ToolSpec(name="update_meal", handler=tool_update_meal, ref_type="meal"),
    "delete_meal": ToolSpec(name="delete_meal", handler=tool_delete_meal, destructive=True),
    "get_plan": ToolSpec(name="get_plan", handler=tool_get_plan, ref_type="plan"),
    "create_plan": ToolSpec(name="create_plan", handler=tool_create_plan, ref_type="plan"),
    "add_meal_to_plan": ToolSpec(name="add_meal_to_plan", handler=tool_add_meal_to_plan, ref_type="plan"),
    "remove_meal_from_plan": ToolSpec(
        name="remove_meal_from_plan", handler=tool_remove_meal_from_plan, ref_type="plan"
    ),
    "mark_meal_cooked": ToolSpec(name="mark_meal_cooked", handler=tool_mark_meal_cooked, ref_type="plan"),
    "undo_meal_cooked": ToolSpec(name="undo_meal_cooked", handler=tool_undo_meal_cooked, ref_type="plan"),
    "get_shopping_list": ToolSpec(name="get_shopping_list", handler=tool_get_shopping_list),
    "add_to_list": ToolSpec(name="add_to_list", handler=tool_add_to_list, ref_type="list_item"),
    "check_off": ToolSpec(name="check_off", handler=tool_check_off, ref_type="list_item"),
    "mark_already_have": ToolSpec(name="mark_already_have", handler=tool_mark_already_have, ref_type="list_item"),
    "need_staple": ToolSpec(name="need_staple", handler=tool_need_staple, ref_type="list_item"),
    "finish_shop": ToolSpec(name="finish_shop", handler=tool_finish_shop, destructive=True),
    "get_freezer": ToolSpec(name="get_freezer", handler=tool_get_freezer),
    "add_to_freezer": ToolSpec(name="add_to_freezer", handler=tool_add_to_freezer, ref_type="freezer_item"),
    "take_from_freezer": ToolSpec(
        name="take_from_freezer",
        handler=tool_take_from_freezer,
        destructive_for=lambda args: bool(args.get("all_of_it")),
        ref_type="freezer_item",
    ),
    "set_ingredient_aisle": ToolSpec(
        name="set_ingredient_aisle", handler=tool_set_ingredient_aisle, ref_type="ingredient"
    ),
    "set_ingredient_value": ToolSpec(
        name="set_ingredient_value", handler=tool_set_ingredient_value, ref_type="ingredient"
    ),
    "list_ingredients_by_value": ToolSpec(name="list_ingredients_by_value", handler=tool_list_ingredients_by_value),
    "find_duplicate_ingredients": ToolSpec(name="find_duplicate_ingredients", handler=tool_find_duplicate_ingredients),
    "merge_ingredients": ToolSpec(
        name="merge_ingredients", handler=tool_merge_ingredients, destructive=True, ref_type="ingredient"
    ),
    "delete_ingredient": ToolSpec(name="delete_ingredient", handler=tool_delete_ingredient, destructive=True),
    "list_supermarkets": ToolSpec(name="list_supermarkets", handler=tool_list_supermarkets),
    "switch_supermarket": ToolSpec(name="switch_supermarket", handler=tool_switch_supermarket, ref_type="supermarket"),
    "save_supermarket": ToolSpec(name="save_supermarket", handler=tool_save_supermarket, ref_type="supermarket"),
    "check_limits": ToolSpec(name="check_limits", handler=tool_check_limits),
}


async def tool_definitions() -> list[dict[str, Any]]:
    """The wire definitions for the tools this assistant serves, built from
    the MCP server's own tool list — names, descriptions and JSON schemas
    are never written twice. Cached per process: they change only with a
    deploy, which restarts the process anyway."""
    global _definitions_cache
    if _definitions_cache is not None:
        return _definitions_cache
    from meals_mcp import server as mcp_server

    tools = await mcp_server.mcp.list_tools()
    _definitions_cache = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or tool.name,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
        if tool.name in TOOLS
    ]
    return _definitions_cache


_definitions_cache: list[dict[str, Any]] | None = None


def forget_definitions() -> None:
    """Drop the cache — for tests that run before the MCP tools exist, or
    that add one (the update_recipe step) and need the registry to re-read."""
    global _definitions_cache
    _definitions_cache = None


#: JSON-schema types the argument validator understands, mapped to the
#: Python types that satisfy them. Anything the schema says that is not in
#: here (anyOf, oneOf…) is left unpoliced — the handler's own pydantic
#: models are the second, stricter gate anyway.
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_arguments(schema: dict[str, Any], arguments: Any) -> str | None:
    """Check the model's arguments against the tool's JSON schema. Returns an
    error sentence for the model to fix, or None when they fit.

    Deliberately shallow: required presence, basic types, enums. The
    handlers' pydantic payloads do the strict validation, and their errors
    reach the model as tool results all the same — this pass exists so the
    model learns "wrong shape" before the tool runs, not from inside it.
    """
    if not isinstance(arguments, dict):
        return "tool arguments must be a JSON object"
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if arguments.get(name) is None:
            return f"missing required argument '{name}'"
    for name, value in arguments.items():
        prop = properties.get(name)
        if prop is None:
            continue  # unknown keys harm nobody; handlers read by name
        enum = prop.get("enum")
        if enum and value not in enum:
            return f"argument '{name}' must be one of {enum}"
        expected = prop.get("type")
        allowed = _JSON_TYPES.get(expected)
        if allowed is None:
            continue
        if not isinstance(value, allowed) or (expected in ("number", "integer") and isinstance(value, bool)):
            article = "an" if expected[0] in "aeiou" else "a"
            return f"argument '{name}' must be {article} {expected}"
    return None


#: The household-facing sentence for each destructive tool. These are shown
#: in the app's native confirmation dialog, so they are product copy — and
#: they deliberately mirror what SKILL.md's "when to ask vs act" section
#: already tells an assistant to ask about.
_SUMMARIES: dict[str, str] = {
    "delete_recipe": "Delete the recipe '{title}' from the library permanently?",
    "delete_meal": "Delete the meal '{meal_name}'? It comes off any active plan and its shopping-list lines are removed.",
    "delete_ingredient": "Delete the ingredient '{name}' from the catalogue permanently?",
    "merge_ingredients": "Fold ingredients into '{keep}'? Merging is not reversible.",
    "finish_shop": "Archive the current shopping list and start a fresh one?",
    "reparse_recipe": "Re-parse '{recipe}' from its source page, discarding any edits made here?",
    "take_from_freezer": "Remove every batch of '{name}' from the freezer?",
}


def confirmation_summary(name: str, arguments: dict[str, Any]) -> str:
    template = _SUMMARIES.get(name)
    if template:
        try:
            return template.format(**arguments)
        except (KeyError, IndexError):
            pass
    return f"Run the '{name}' action? It is hard to undo."
