import json
from collections import defaultdict

import frappe
from frappe import _, bold, throw
from frappe.query_builder.functions import Sum
from frappe.utils import cint, cstr, flt, get_link_to_form, nowdate

from erpnext.manufacturing.doctype.bom.bom import (
	add_additional_cost,
	get_backflush_based_on,
	get_bom_items_as_dict,
	get_secondary_items_from_sub_assemblies,
)
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
from erpnext.stock.get_item_details import get_item_defaults
from erpnext.stock.serial_batch_bundle import (
	SerialBatchCreation,
	get_batch_nos,
	get_empty_batches_based_work_order,
	get_serial_or_batch_items,
)
from erpnext.stock.utils import get_combine_datetime


class OperationsNotCompleteError(frappe.ValidationError):
	pass


class DuplicateEntryForWorkOrderError(frappe.ValidationError):
	pass


class BaseManufacturingHandler:
	def __init__(self, se_doc):
		self.se_doc = se_doc

	def validate(self):
		self.validate_posting_date_and_time()

		if self.se_doc.purpose in ["Manufacture", "Material Consumption for Manufacture"]:
			self.validate_work_order()

	def get_secondary_items(self):
		qty = self.se_doc.fg_completed_qty or 0

		if (
			frappe.db.get_single_value(
				"Manufacturing Settings", "set_op_cost_and_secondary_items_from_sub_assemblies"
			)
			and self.se_doc.work_order
			and frappe.get_cached_value("Work Order", self.se_doc.work_order, "use_multi_level_bom")
		):
			item_dict = get_secondary_items_from_sub_assemblies(self.se_doc.bom_no, self.se_doc.company, qty)
		else:
			# item dict = { item_code: {qty, description, stock_uom} }
			item_dict = (
				get_bom_items_as_dict(
					self.se_doc.bom_no,
					self.se_doc.company,
					qty=qty,
					fetch_exploded=0,
					fetch_secondary_items=1,
				)
				or {}
			)

		for item in item_dict.values():
			item.from_warehouse = ""

		return item_dict

	def get_completed_job_card_qty(self):
		return flt(min([d.completed_qty for d in self.wo_doc.operations]))

	def add_to_stock_entry_detail(self, item_dict, bom_no=None):
		from erpnext.stock.get_item_details import get_default_cost_center

		precision = frappe.get_precision("Stock Entry Detail", "qty")
		for d in item_dict:
			item_row = item_dict[d]

			child_qty = flt(item_row["qty"], precision)
			if (
				not self.se_doc.is_return
				and child_qty <= 0
				and not item_row.get("type")
				and not item_row.get("is_legacy_scrap_item")
			):
				if self.se_doc.purpose not in ["Receive from Customer", "Send to Subcontractor"]:
					continue

			se_child = self.se_doc.append("items")
			stock_uom = item_row.get("stock_uom") or frappe.db.get_value("Item", d, "stock_uom")
			se_child.s_warehouse = item_row.get("from_warehouse")
			se_child.t_warehouse = item_row.get("to_warehouse")
			se_child.item_code = item_row.get("item_code") or cstr(d)
			se_child.uom = item_row["uom"] if item_row.get("uom") else stock_uom
			se_child.stock_uom = stock_uom
			se_child.qty = child_qty if child_qty > 0 else 0
			se_child.allow_alternative_item = item_row.get("allow_alternative_item", 0)
			se_child.subcontracted_item = item_row.get("main_item_code")
			se_child.cost_center = item_row.get("cost_center") or get_default_cost_center(
				item_row, company=self.se_doc.company
			)
			se_child.is_finished_item = item_row.get("is_finished_item", 0)
			se_child.po_detail = item_row.get("po_detail")
			se_child.sco_rm_detail = item_row.get("sco_rm_detail")
			se_child.scio_detail = item_row.get("scio_detail")
			se_child.sample_quantity = item_row.get("sample_quantity", 0)
			se_child.type = item_row.get("type")
			se_child.is_legacy_scrap_item = item_row.get("is_legacy")
			se_child.bom_secondary_item = item_row.get("name") or item_row.get("bom_secondary_item")

			for field in [
				self.se_doc.subcontract_data.rm_detail_field,
				"original_item",
				"expense_account",
				"description",
				"item_name",
				"serial_and_batch_bundle",
				"allow_zero_valuation_rate",
				"use_serial_batch_fields",
				"batch_no",
				"serial_no",
			]:
				if item_row.get(field):
					se_child.set(field, item_row.get(field))

			if se_child.s_warehouse is None:
				se_child.s_warehouse = self.se_doc.from_warehouse
			if se_child.t_warehouse is None:
				se_child.t_warehouse = self.se_doc.to_warehouse

			# in stock uom
			se_child.conversion_factor = flt(item_row.get("conversion_factor")) or 1
			se_child.transfer_qty = flt(
				item_row["qty"] * se_child.conversion_factor, se_child.precision("qty")
			)

			se_child.bom_no = bom_no  # to be assigned for finished item
			se_child.job_card_item = item_row.get("job_card_item") if self.se_doc.get("job_card") else None

	def validate_posting_date_and_time(self):
		if not self.se_doc.posting_date or not self.se_doc.posting_time:
			frappe.throw(_("Posting date and posting time is mandatory"))

	def validate_work_order(self):
		if self.se_doc.purpose in (
			"Manufacture",
			"Material Transfer for Manufacture",
			"Material Consumption for Manufacture",
			"Disassemble",
		):
			# check if work order is entered

			if (
				(
					self.se_doc.purpose == "Manufacture"
					or self.se_doc.purpose == "Material Consumption for Manufacture"
				)
				and self.se_doc.work_order
				and frappe.get_cached_value("Work Order", self.se_doc.work_order, "track_semi_finished_goods")
				!= 1
			):
				if not self.se_doc.fg_completed_qty:
					frappe.throw(_("For Quantity (Manufactured Qty) is mandatory"))

				self.check_if_operations_completed()
				self.check_duplicate_entry_for_work_order()
		elif self.se_doc.purpose != "Material Transfer":
			self.se_doc.work_order = None

	def check_if_operations_completed(self):
		"""Ensure all operations are completed before manufacturing to capture operating costs."""

		work_order = self.wo_doc
		precision = self.se_doc.precision("fg_completed_qty")

		allowance_percentage = frappe.db.get_single_value(
			"Manufacturing Settings", "overproduction_percentage_for_work_order"
		)

		total_completed_qty = flt(self.se_doc.fg_completed_qty) + flt(work_order.produced_qty)

		for op in work_order.get("operations"):
			allowed_qty = (
				flt(op.completed_qty)
				+ flt(op.process_loss_qty)
				+ (allowance_percentage / 100) * flt(op.completed_qty)
			)

			if flt(total_completed_qty, precision) <= flt(allowed_qty, precision):
				continue

			job_card = frappe.db.get_value("Job Card", {"operation_id": op.name}, "name")

			if not job_card:
				throw(
					_("Work Order {0}: Job Card not found for operation {1}").format(
						self.se_doc.work_order, op.operation
					)
				)

			throw(
				_(
					"Row #{row}: Operation {operation} is not completed for {qty} qty "
					"in Work Order {wo}. Please update it via Job Card {jc}."
				).format(
					row=op.idx,
					operation=bold(op.operation),
					qty=bold(total_completed_qty),
					wo=get_link_to_form("Work Order", self.se_doc.work_order),
					jc=get_link_to_form("Job Card", job_card),
				),
				OperationsNotCompleteError,
			)

	def get_stock_entries_for_work_order(self):
		return frappe.get_all(
			"Stock Entry",
			filters={
				"work_order": self.se_doc.work_order,
				"purpose": self.se_doc.purpose,
				"docstatus": ["!=", 2],
				"name": ["!=", self.se_doc.name],
			},
			pluck="name",
		)

	def get_transfer_qty(self, production_item):
		self.other_stock_entries = self.get_stock_entries_for_work_order() or []
		if not self.other_stock_entries:
			return 0

		doctype = frappe.qb.DocType("Stock Entry Detail")
		query = (
			frappe.qb.from_(doctype)
			.select(Sum(doctype.transfer_qty))
			.where(
				(doctype.parent.isin(self.other_stock_entries))
				& (doctype.item_code == production_item)
				& (doctype.s_warehouse.isnull() | (doctype.s_warehouse == ""))
			)
		).run()

		return flt(query[0][0]) if query and query[0][0] else 0

	def check_duplicate_entry_for_work_order(self):
		production_item, qty = frappe.db.get_value(
			"Work Order", self.se_doc.work_order, ["production_item", "qty"]
		)

		fg_qty_already_entered = self.get_transfer_qty(production_item)
		if fg_qty_already_entered and fg_qty_already_entered >= qty:
			frappe.throw(
				_("Stock Entries already created for Work Order {0}: {1}").format(
					self.se_doc.work_order, ", ".join(self.other_stock_entries)
				),
				DuplicateEntryForWorkOrderError,
			)

	def add_finished_item_from_bom(self):
		if self.wo_doc:
			item_code = self.wo_doc.production_item
			to_warehouse = self.wo_doc.fg_warehouse
		else:
			item_code = frappe.db.get_value("BOM", self.se_doc.bom_no, "item")
			to_warehouse = self.se_doc.to_warehouse

		item = get_item_defaults(item_code, self.se_doc.company)

		if not self.se_doc.work_order and not to_warehouse:
			# in case of BOM
			to_warehouse = item.get("default_warehouse")

		expense_account = item.get("expense_account")
		if not expense_account:
			expense_account = frappe.get_cached_value(
				"Company", self.se_doc.company, "stock_adjustment_account"
			)

		args = {
			"to_warehouse": to_warehouse,
			"from_warehouse": "",
			"qty": flt(self.se_doc.fg_completed_qty) - flt(self.se_doc.process_loss_qty),
			"item_name": item.item_name,
			"description": item.description,
			"stock_uom": item.stock_uom,
			"expense_account": expense_account,
			"cost_center": item.get("buying_cost_center"),
			"is_finished_item": 1,
			"sample_quantity": item.get("sample_quantity"),
		}

		if self.se_doc.purpose == "Disassemble":
			args.update(
				{
					"from_warehouse": self.se_doc.from_warehouse,
					"to_warehouse": "",
					"qty": flt(self.se_doc.fg_completed_qty),
				}
			)

		if (
			self.wo_doc
			and self.wo_doc.has_batch_no
			and not self.wo_doc.has_serial_no
			and cint(
				frappe.db.get_single_value(
					"Manufacturing Settings", "make_serial_no_batch_from_work_order", cache=True
				)
			)
		):
			self.set_batchwise_finished_goods(args, item)
		else:
			self.add_finished_goods(args, item)

	def set_batchwise_finished_goods(self, args, item):
		batches = get_empty_batches_based_work_order(
			self.se_doc.work_order, self.se_doc.pro_doc.production_item
		)

		if not batches:
			self.add_finished_goods(args, item)
		else:
			self.add_batchwise_finished_good(batches, args, item)

	def add_batchwise_finished_good(self, batches, args, item):
		qty = flt(self.se_doc.fg_completed_qty)
		row = frappe._dict({"batches_to_be_consume": defaultdict(float)})

		self.update_batches_to_be_consume(batches, row, qty)

		if not row.batches_to_be_consume:
			return

		_id = create_serial_and_batch_bundle(
			self.se_doc,
			row,
			frappe._dict(
				{
					"item_code": self.se_doc.pro_doc.production_item,
					"warehouse": args.get("to_warehouse"),
				}
			),
		)

		args["serial_and_batch_bundle"] = _id
		self.add_finished_goods(args, item)

	def add_finished_goods(self, args, item):
		self.add_to_stock_entry_detail({item.name: args}, bom_no=self.se_doc.bom_no)

	@property
	def wo_doc(self):
		if not getattr(self, "_wo_doc", None):
			if self.se_doc.work_order:
				self._wo_doc = frappe.get_doc("Work Order", self.se_doc.work_order)
		return getattr(self, "_wo_doc", None)

	@property
	def backflush_based_on(self):
		return get_backflush_based_on(self.se_doc.bom_no)

	def reset(self):
		self._wo_doc = None
		self._backflush_based_on = None


class ManufactureHandler(BaseManufacturingHandler):
	def set_items(self):
		self.validate_fg_completed_qty()

		if self.se_doc.purpose in ["Manufacture", "Material Consumption for Manufacture"]:
			if (
				self.wo_doc
				and not self.wo_doc.skip_transfer
				and self.backflush_based_on == "Material Transferred for Manufacture"
			):
				self.add_transfered_raw_materials_in_items()
			elif (
				self.backflush_based_on == "BOM"
				and frappe.db.get_single_value("Manufacturing Settings", "material_consumption") == 1
			):
				self.get_unconsumed_raw_materials()
			else:
				self.add_raw_materials_from_bom()

			self.set_serial_no_for_finished_good()
		else:
			self.add_raw_materials_from_bom()

		if self.se_doc.purpose in ["Manufacture", "Repack"]:
			self.process_loss_qty()
			self.add_finished_item_from_bom()

		self.set_secondary_items()
		if not self.wo_doc:
			return

		self.add_additional_cost()
		self.set_secondary_items_from_job_card()

	def get_available_serial_nos_for_fg(self) -> list[str]:
		return frappe.get_all(
			"Serial No",
			filters={
				"item_code": self.wo_doc.production_item,
				"warehouse": ("is", "not set"),
				"status": "Inactive",
				"work_order": self.wo_doc.name,
			},
			pluck="name",
			order_by="creation asc",
		)

	def set_serial_no_for_finished_good(self):
		if not self.wo_doc:
			return

		if not (
			(self.wo_doc.has_serial_no or self.wo_doc.has_batch_no)
			and frappe.db.get_single_value("Manufacturing Settings", "make_serial_no_batch_from_work_order")
		):
			return

		for d in self.se_doc.items:
			if (
				d.is_finished_item
				and d.item_code == self.wo_doc.production_item
				and not d.serial_and_batch_bundle
			):
				serial_nos = self.get_available_serial_nos_for_fg()
				if serial_nos:
					row = frappe._dict({"serial_nos": serial_nos[0 : cint(d.qty)]})

					_id = create_serial_and_batch_bundle(
						self.se_doc,
						row,
						frappe._dict(
							{
								"item_code": d.item_code,
								"warehouse": d.t_warehouse,
							}
						),
					)

					d.serial_and_batch_bundle = _id
					d.use_serial_batch_fields = 0

	def set_secondary_items(self):
		if self.se_doc.purpose not in ["Manufacture", "Repack"]:
			return

		secondary_items_dict = self.get_secondary_items()
		for item in secondary_items_dict.values():
			if self.wo_doc and item.type:
				if self.wo_doc.scrap_warehouse and item.type == "Scrap":
					item["to_warehouse"] = self.wo_doc.scrap_warehouse

			if item.process_loss_per:
				item["qty"] -= flt(
					item["qty"] * (item.process_loss_per / 100),
					self.se_doc.precision("fg_completed_qty"),
				)

		self.add_to_stock_entry_detail(secondary_items_dict, bom_no=self.se_doc.bom_no)

	def set_secondary_items_from_job_card(self):
		if self.se_doc.purpose not in ["Manufacture", "Repack"]:
			return

		item_dict = {}
		for row in self.get_secondary_items_from_job_card():
			if row.stock_qty <= 0:
				continue

			item_dict[row.item_code] = frappe._dict(
				{
					"uom": row.stock_uom,
					"from_warehouse": "",
					"qty": row.stock_qty,
					"conversion_factor": 1,
					"type": row.type,
					"item_name": row.item_name,
					"description": row.description,
					"bom_secondary_item": row.bom_secondary_item,
				}
			)

		for item in item_dict.values():
			item.from_warehouse = ""

		self.add_to_stock_entry_detail(item_dict)

	def get_secondary_items_from_job_card(self):
		if not self.wo_doc.operations:
			return []

		job_card = frappe.qb.DocType("Job Card")
		job_card_secondary_item = frappe.qb.DocType("Job Card Secondary Item")

		other = (
			frappe.qb.from_(job_card)
			.select(
				Sum(job_card_secondary_item.stock_qty).as_("stock_qty"),
				job_card_secondary_item.item_code,
				job_card_secondary_item.item_name,
				job_card_secondary_item.description,
				job_card_secondary_item.stock_uom,
				job_card_secondary_item.type,
				job_card_secondary_item.bom_secondary_item,
			)
			.join(job_card_secondary_item)
			.on(job_card_secondary_item.parent == job_card.name)
			.where(
				(job_card_secondary_item.item_code.isnotnull())
				& (job_card.work_order == self.se_doc.work_order)
				& (job_card.docstatus == 1)
			)
			.groupby(job_card_secondary_item.item_code, job_card_secondary_item.type)
			.orderby(job_card_secondary_item.idx)
		)

		if self.se_doc.job_card:
			other = other.where(job_card.name == self.se_doc.job_card)

		other = other.run(as_dict=1)

		if self.se_doc.job_card:
			pending_qty = flt(self.se_doc.fg_completed_qty)
		else:
			pending_qty = flt(self.get_completed_job_card_qty()) - flt(self.wo_doc.produced_qty)

		used_secondary_items = self.get_used_secondary_items()
		for row in other:
			row.stock_qty -= flt(used_secondary_items.get(row.item_code))
			row.stock_qty = (row.stock_qty) * flt(self.se_doc.fg_completed_qty) / flt(pending_qty)

			if used_secondary_items.get(row.item_code):
				used_secondary_items[row.item_code] -= row.stock_qty

			if cint(frappe.get_cached_value("UOM", row.stock_uom, "must_be_whole_number")):
				row.stock_qty = frappe.utils.ceil(row.stock_qty)

		return other

	def get_used_secondary_items(self):
		used_secondary_items = defaultdict(float)

		StockEntry = frappe.qb.DocType("Stock Entry")
		StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
		data = (
			frappe.qb.from_(StockEntry)
			.inner_join(StockEntryDetail)
			.on(StockEntryDetail.parent == StockEntry.name)
			.select(StockEntryDetail.item_code, StockEntryDetail.qty)
			.where(
				(StockEntry.work_order == self.se_doc.work_order)
				& ((StockEntryDetail.type.isnotnull()) | (StockEntryDetail.is_legacy_scrap_item == 1))
				& (StockEntry.docstatus == 1)
				& (StockEntry.purpose.isin(["Repack", "Manufacture"]))
			)
		).run(as_dict=1)

		for row in data:
			used_secondary_items[row.item_code] += row.qty

		return used_secondary_items

	def process_loss_qty(self):
		if self.se_doc.purpose not in ("Manufacture", "Repack"):
			return

		self.se_doc.set_process_loss_qty()

	def add_additional_cost(self):
		if self.se_doc.purpose != "Manufacture":
			return

		add_additional_cost(self.se_doc, self.wo_doc)

	def add_raw_materials_from_bom(self):
		item_dict = self.se_doc.get_bom_raw_materials(self.se_doc.fg_completed_qty)
		item_wh = self.get_subcontract_order_supplied_items()
		for original_item, item in item_dict.items():
			if self.wo_doc and cint(self.wo_doc.from_wip_warehouse):
				item["from_warehouse"] = self.wo_doc.wip_warehouse

			# Get Reserve Warehouse from Subcontract Order
			if (
				self.se_doc.get(self.se_doc.subcontract_data.order_field)
				and self.se_doc.purpose == "Send to Subcontractor"
			):
				item["from_warehouse"] = item_wh.get(item.item_code)

			item["to_warehouse"] = (
				self.se_doc.to_warehouse if self.se_doc.purpose == "Send to Subcontractor" else ""
			)

			if isinstance(original_item, str) and original_item != item.get("item_code"):
				item["original_item"] = original_item

		self.add_to_stock_entry_detail(item_dict)

	def get_subcontract_order_supplied_items(self):
		item_wh = frappe._dict()
		# Get Subcontract Order Supplied Items Details
		if (
			self.se_doc.get(self.se_doc.subcontract_data.order_field)
			and self.se_doc.purpose == "Send to Subcontractor"
		):
			# Get Subcontract Order Supplied Items Details
			parent = frappe.qb.DocType(self.se_doc.subcontract_data.order_doctype)
			child = frappe.qb.DocType(self.se_doc.subcontract_data.order_supplied_items_field)

			item_wh = (
				frappe.qb.from_(parent)
				.inner_join(child)
				.on(parent.name == child.parent)
				.select(child.rm_item_code, child.reserve_warehouse)
				.where(parent.name == self.se_doc.get(self.se_doc.subcontract_data.order_field))
			).run(as_list=True)

			item_wh = frappe._dict(item_wh)

		return item_wh

	def validate_fg_completed_qty(self):
		if not self.se_doc.fg_completed_qty:
			frappe.throw(_("Manufacturing Quantity is mandatory"))

	def get_unconsumed_raw_materials(self):
		wo = self.wo_doc

		work_order_qty = wo.material_transferred_for_manufacturing or wo.qty
		for item in wo.get("required_items"):
			item_account_details = get_item_defaults(item.item_code, self.se_doc.company)
			# Take into account consumption if there are any.

			wo_item_qty = item.transferred_qty or item.required_qty

			wo_qty_unconsumed = flt(wo_item_qty) - flt(item.consumed_qty)
			wo_qty_to_produce = flt(work_order_qty) - flt(wo.produced_qty)
			bom_qty_per_unit = item.required_qty / wo.qty  # per-unit BOM qty

			req_qty_each = (wo_qty_unconsumed) / (wo_qty_to_produce or 1)
			req_qty_each = min(req_qty_each, bom_qty_per_unit)

			qty = req_qty_each * flt(self.se_doc.fg_completed_qty)

			if qty > 0:
				self.add_to_stock_entry_detail(
					{
						item.item_code: {
							"from_warehouse": wo.wip_warehouse or item.source_warehouse,
							"to_warehouse": "",
							"qty": qty,
							"item_name": item.item_name,
							"description": item.description,
							"stock_uom": item_account_details.stock_uom,
							"expense_account": item_account_details.get("expense_account"),
							"cost_center": item_account_details.get("buying_cost_center"),
						}
					}
				)

	def add_transfered_raw_materials_in_items(self) -> None:
		available_materials = get_available_materials(self.se_doc.work_order)
		wo_data = self.wo_doc

		precision = frappe.get_precision("Stock Entry Detail", "qty")
		for _key, row in available_materials.items():
			remaining_qty_to_produce = flt(wo_data.material_transferred_for_manufacturing) - flt(
				wo_data.produced_qty
			)
			if remaining_qty_to_produce <= 0 and not self.se_doc.is_return:
				continue

			qty = flt(row.qty)
			if not self.se_doc.is_return:
				qty = (flt(row.qty) * flt(self.se_doc.fg_completed_qty)) / remaining_qty_to_produce

			item = row.item_details
			if cint(frappe.get_cached_value("UOM", item.stock_uom, "must_be_whole_number")):
				qty = frappe.utils.ceil(qty)

			if row.batch_details:
				row.batches_to_be_consume = defaultdict(float)
				batches = row.batch_details
				self.update_batches_to_be_consume(batches, row, qty)

			elif row.serial_nos:
				serial_nos = row.serial_nos[0 : cint(qty)]
				row.serial_nos = serial_nos

			if flt(qty, precision) != 0.0:
				self.update_item_in_stock_entry_detail(row, item, qty)

	@staticmethod
	def get_serial_nos_based_on_transferred_batch(batch_no, serial_nos) -> list:
		return frappe.get_all(
			"Serial No",
			filters={"batch_no": batch_no, "name": ("in", serial_nos), "warehouse": ("is", "not set")},
			pluck="name",
			order_by="creation",
		)

	def update_batches_to_be_consume(self, batches, row, qty):
		qty_to_be_consumed = qty
		batches = sorted(batches.items(), key=lambda x: x[0])

		for batch_no, batch_qty in batches:
			if qty_to_be_consumed <= 0 or batch_qty <= 0:
				continue

			if batch_qty > qty_to_be_consumed:
				batch_qty = qty_to_be_consumed

			row.batches_to_be_consume[batch_no] += batch_qty

			if batch_no and row.serial_nos:
				serial_nos = self.get_serial_nos_based_on_transferred_batch(batch_no, row.serial_nos)
				serial_nos = serial_nos[0 : cint(batch_qty)]

				# remove consumed serial nos from list
				for sn in serial_nos:
					row.serial_nos.remove(sn)

			if "batch_details" in row:
				row.batch_details[batch_no] -= batch_qty

			qty_to_be_consumed -= batch_qty

	def update_item_in_stock_entry_detail(self, row, item, qty) -> None:
		if not qty:
			return

		use_serial_batch_fields = frappe.get_single_value("Stock Settings", "use_serial_batch_fields")

		ste_item_details = {
			"from_warehouse": item.warehouse,
			"to_warehouse": "",
			"qty": qty,
			"item_name": item.item_name,
			"serial_and_batch_bundle": create_serial_and_batch_bundle(self.se_doc, row, item, "Outward")
			if not use_serial_batch_fields
			else "",
			"description": item.description,
			"stock_uom": item.stock_uom,
			"expense_account": item.expense_account,
			"cost_center": item.buying_cost_center,
			"original_item": item.original_item,
			"serial_no": "\n".join(row.serial_nos)
			if row.serial_nos and not row.batches_to_be_consume
			else "",
			"use_serial_batch_fields": use_serial_batch_fields,
		}

		if self.se_doc.is_return:
			ste_item_details["to_warehouse"] = item.s_warehouse

		if use_serial_batch_fields and not row.serial_no and row.batches_to_be_consume:
			for batch_no, batch_qty in row.batches_to_be_consume.items():
				ste_item_details.update(
					{
						"batch_no": batch_no,
						"qty": batch_qty,
					}
				)

				if row.serial_nos:
					serial_nos = row.serial_nos[0 : cint(batch_qty)]
					ste_item_details["serial_no"] = "\n".join(serial_nos)

					row.serial_nos = [sn for sn in row.serial_nos if sn not in serial_nos]

				self.add_to_stock_entry_detail({item.item_code: ste_item_details})
		else:
			self.add_to_stock_entry_detail({item.item_code: ste_item_details})


class MaterialTransferHandler(BaseManufacturingHandler):
	# This class is for stock entry type 'Material Transfer for Manufacture'

	def set_items(self):
		item_dict = self.get_pending_raw_materials()
		if self.se_doc.to_warehouse and self.wo_doc:
			for item in item_dict.values():
				item["to_warehouse"] = self.wo_doc.wip_warehouse
		self.add_to_stock_entry_detail(item_dict)

	def get_pending_raw_materials(self):
		"""
		issue (item quantity) that is pending to issue or desire to transfer,
		whichever is less
		"""
		item_dict = self.get_work_order_required_items()

		max_qty = flt(self.wo_doc.qty)

		allow_overproduction = False
		overproduction_percentage = flt(
			frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_work_order")
		)

		transfer_extra_materials_percentage = flt(
			frappe.db.get_single_value("Manufacturing Settings", "transfer_extra_materials_percentage")
		)

		to_transfer_qty = flt(self.wo_doc.material_transferred_for_manufacturing) + flt(
			self.se_doc.fg_completed_qty
		)
		transfer_limit_qty = max_qty + ((max_qty * overproduction_percentage) / 100)
		if transfer_extra_materials_percentage:
			transfer_limit_qty = max_qty + ((max_qty * transfer_extra_materials_percentage) / 100)

		if transfer_limit_qty >= to_transfer_qty:
			allow_overproduction = True

		for item, item_details in item_dict.items():
			pending_to_issue = flt(item_details.required_qty) - flt(item_details.transferred_qty)
			desire_to_transfer = flt(self.se_doc.fg_completed_qty) * flt(item_details.required_qty) / max_qty

			if (
				desire_to_transfer <= pending_to_issue
				or (
					desire_to_transfer > 0
					and self.backflush_based_on == "Material Transferred for Manufacture"
				)
				or allow_overproduction
			):
				# "No need for transfer but qty still pending to transfer" case can occur
				# when transferring multiple RM in different Stock Entries
				item_dict[item]["qty"] = desire_to_transfer if (desire_to_transfer > 0) else pending_to_issue
			elif pending_to_issue > 0:
				item_dict[item]["qty"] = pending_to_issue
			else:
				item_dict[item]["qty"] = 0

		# delete items with 0 qty
		list_of_items = list(item_dict.keys())
		for item in list_of_items:
			if not item_dict[item]["qty"]:
				del item_dict[item]

		# show some message
		if not len(item_dict):
			frappe.msgprint(_("""All items have already been transferred for this Work Order."""))

		return item_dict

	def get_work_order_required_items(self):
		"""
		Gets Work Order Required Items only if Stock Entry purpose is **Material Transferred for Manufacture**.
		"""
		item_dict, job_card_items = frappe._dict(), []
		work_order = self.wo_doc

		consider_job_card = work_order.transfer_material_against == "Job Card" and self.se_doc.get("job_card")
		if consider_job_card:
			job_card_items = self.get_job_card_item_codes()

		if not frappe.db.get_value("Warehouse", work_order.wip_warehouse, "is_group"):
			wip_warehouse = work_order.wip_warehouse
		else:
			wip_warehouse = None

		transfer_extra_materials_percentage = flt(
			frappe.db.get_single_value("Manufacturing Settings", "transfer_extra_materials_percentage")
		)

		for d in work_order.get("required_items"):
			if consider_job_card and (d.item_code not in job_card_items):
				continue

			additional_qty = 0.0
			if transfer_extra_materials_percentage:
				additional_qty = transfer_extra_materials_percentage * flt(d.required_qty) / 100

			transfer_pending = flt(d.required_qty) > flt(d.transferred_qty)
			if additional_qty:
				transfer_pending = (flt(d.required_qty) + additional_qty) > flt(d.transferred_qty)

			can_transfer = transfer_pending or (
				self.backflush_based_on == "Material Transferred for Manufacture"
			)

			if not can_transfer:
				continue

			if d.include_item_in_manufacturing:
				item_row = d.as_dict()
				item_row["idx"] = len(item_dict) + 1

				if consider_job_card:
					job_card_item = frappe.db.get_value(
						"Job Card Item", {"item_code": d.item_code, "parent": self.se_doc.get("job_card")}
					)
					item_row["job_card_item"] = job_card_item or None

				if d.source_warehouse and not frappe.db.get_value(
					"Warehouse", d.source_warehouse, "is_group"
				):
					item_row["from_warehouse"] = d.source_warehouse

				item_row["to_warehouse"] = wip_warehouse
				if item_row["allow_alternative_item"]:
					item_row["allow_alternative_item"] = work_order.allow_alternative_item

				item_dict.setdefault(d.item_code, item_row)

		return item_dict

	def get_job_card_item_codes(self):
		if not self.se_doc.get("job_card"):
			return []

		return frappe.get_all(
			"Job Card Item", filters={"parent": self.se_doc.get("job_card")}, pluck="item_code", distinct=True
		)


class DisassembleHandler(BaseManufacturingHandler):
	def set_items(self):
		"""
		Priority:
		1. From a specific Manufacture Stock Entry (exact reversal)
		2. From Work Order Manufacture Stock Entries (averaged reversal)
		3. From BOM (standalone disassembly)
		"""

		# Auto-set source_stock_entry if WO has exactly one manufacture entry
		if not self.se_doc.get("source_stock_entry") and self.se_doc.work_order:
			manufacture_entries = frappe.get_all(
				"Stock Entry",
				filters={
					"work_order": self.se_doc.work_order,
					"purpose": "Manufacture",
					"docstatus": 1,
				},
				pluck="name",
				limit_page_length=2,
			)
			if len(manufacture_entries) == 1:
				self.se_doc.source_stock_entry = manufacture_entries[0]

		if self.se_doc.get("source_stock_entry"):
			return self._add_items_for_disassembly_from_stock_entry()

		if self.se_doc.work_order:
			return self._add_items_for_disassembly_from_work_order()

		return self._add_items_for_disassembly_from_bom()

	def _add_items_for_disassembly_from_stock_entry(self):
		source_fg_qty = frappe.db.get_value("Stock Entry", self.se_doc.source_stock_entry, "fg_completed_qty")
		if not source_fg_qty:
			frappe.throw(
				_("Source Stock Entry {0} has no finished goods quantity").format(
					self.se_doc.source_stock_entry
				)
			)

		disassemble_qty = flt(self.se_doc.fg_completed_qty)
		scale_factor = disassemble_qty / flt(source_fg_qty)

		self._append_disassembly_row_from_source(
			disassemble_qty=disassemble_qty,
			scale_factor=scale_factor,
		)

	def _add_items_for_disassembly_from_work_order(self):
		wo_produced_qty = frappe.db.get_value("Work Order", self.se_doc.work_order, "produced_qty")

		wo_produced_qty = flt(wo_produced_qty)
		if wo_produced_qty <= 0:
			frappe.throw(_("Work Order {0} has no produced qty").format(self.se_doc.work_order))

		disassemble_qty = flt(self.se_doc.fg_completed_qty)
		if disassemble_qty <= 0:
			frappe.throw(_("Disassemble Qty cannot be less than or equal to 0."))

		scale_factor = disassemble_qty / wo_produced_qty

		self._append_disassembly_row_from_source(
			disassemble_qty=disassemble_qty,
			scale_factor=scale_factor,
		)

	def _append_disassembly_row_from_source(self, disassemble_qty, scale_factor):
		for source_row in self.get_items_from_manufacture_stock_entry():
			if source_row.is_finished_item:
				qty = disassemble_qty
				s_warehouse = self.se_doc.from_warehouse or source_row.t_warehouse
				t_warehouse = ""
			elif source_row.s_warehouse:
				# RM: was consumed FROM s_warehouse -> return TO s_warehouse
				qty = flt(source_row.qty * scale_factor)
				s_warehouse = ""
				t_warehouse = self.se_doc.to_warehouse or source_row.s_warehouse
			else:
				# Scrap/secondary: was produced TO t_warehouse -> take FROM t_warehouse
				qty = flt(source_row.qty * scale_factor)
				s_warehouse = source_row.t_warehouse
				t_warehouse = ""

			item = {
				"item_code": source_row.item_code,
				"item_name": source_row.item_name,
				"description": source_row.description,
				"stock_uom": source_row.stock_uom,
				"uom": source_row.uom,
				"conversion_factor": source_row.conversion_factor,
				"basic_rate": source_row.basic_rate,
				"qty": qty,
				"s_warehouse": s_warehouse,
				"t_warehouse": t_warehouse,
				"is_finished_item": source_row.is_finished_item,
				"type": source_row.type,
				"is_legacy_scrap_item": source_row.is_legacy_scrap_item,
				"bom_secondary_item": source_row.bom_secondary_item,
				"bom_no": source_row.bom_no,
				# batch and serial bundles built on submit
				"use_serial_batch_fields": 1 if (source_row.batch_no or source_row.serial_no) else 0,
			}

			if self.se_doc.source_stock_entry:
				item.update(
					{
						"against_stock_entry": self.se_doc.source_stock_entry,
						"ste_detail": source_row.name,
					}
				)

			self.se_doc.append("items", item)

	def _add_items_for_disassembly_from_bom(self):
		if not self.se_doc.bom_no or not self.se_doc.fg_completed_qty:
			frappe.throw(_("BOM and Finished Good Quantity is mandatory for Disassembly"))

		# Raw Materials
		item_dict = self.se_doc.get_bom_raw_materials(self.se_doc.fg_completed_qty)

		for item_row in item_dict.values():
			item_row["to_warehouse"] = self.se_doc.to_warehouse
			item_row["from_warehouse"] = ""
			item_row["is_finished_item"] = 0

		self.add_to_stock_entry_detail(item_dict)

		# Secondary/Scrap items (reverse of what set_secondary_items does for Manufacture)
		secondary_items = self.get_secondary_items()
		if secondary_items:
			scrap_warehouse = self.se_doc.from_warehouse
			if self.se_doc.work_order:
				wo_values = frappe.db.get_value(
					"Work Order", self.se_doc.work_order, ["scrap_warehouse", "fg_warehouse"], as_dict=True
				)
				scrap_warehouse = wo_values.scrap_warehouse or scrap_warehouse or wo_values.fg_warehouse

			for item in secondary_items.values():
				item["from_warehouse"] = scrap_warehouse
				item["to_warehouse"] = ""
				item["is_finished_item"] = 0

				if item.get("process_loss_per"):
					item["qty"] -= flt(
						item["qty"] * (item["process_loss_per"] / 100),
						self.se_doc.precision("fg_completed_qty"),
					)

			self.add_to_stock_entry_detail(secondary_items, bom_no=self.se_doc.bom_no)

		# Finished goods
		self.add_finished_item_from_bom()

	def process_on_submit(self):
		self.set_serial_batch_for_disassembly()

	def set_serial_batch_for_disassembly(self):
		if self.se_doc.purpose != "Disassemble":
			return

		if self.se_doc.get("source_stock_entry"):
			self._set_serial_batch_for_disassembly_from_stock_entry()
		else:
			self._set_serial_batch_for_disassembly_from_available_materials()

	def _set_serial_batch_for_disassembly_from_stock_entry(self):
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
			get_voucher_wise_serial_batch_from_bundle,
		)

		source_fg_qty = flt(
			frappe.db.get_value("Stock Entry", self.se_doc.source_stock_entry, "fg_completed_qty")
		)
		scale_factor = flt(self.se_doc.fg_completed_qty) / source_fg_qty if source_fg_qty else 0

		bundle_data = get_voucher_wise_serial_batch_from_bundle(voucher_no=[self.se_doc.source_stock_entry])
		source_rows_by_name = {r.name: r for r in self.get_items_from_manufacture_stock_entry()}

		for row in self.se_doc.items:
			if not row.ste_detail:
				continue

			source_row = source_rows_by_name.get(row.ste_detail)
			if not source_row:
				continue

			source_warehouse = source_row.s_warehouse or source_row.t_warehouse
			key = (source_row.item_code, source_warehouse, self.se_doc.source_stock_entry)
			source_bundle = bundle_data.get(key, {})

			batches = defaultdict(float)
			serial_nos = []

			if source_bundle.get("batch_nos"):
				qty_remaining = row.transfer_qty
				for batch_no, batch_qty in source_bundle["batch_nos"].items():
					if qty_remaining <= 0:
						break
					alloc = min(abs(flt(batch_qty)) * scale_factor, qty_remaining)
					batches[batch_no] = alloc
					qty_remaining -= alloc
			elif source_row.batch_no:
				batches[source_row.batch_no] = row.transfer_qty

			if source_bundle.get("serial_nos"):
				serial_nos = get_serial_nos(source_bundle["serial_nos"])[: int(row.transfer_qty)]
			elif source_row.serial_no:
				serial_nos = get_serial_nos(source_row.serial_no)[: int(row.transfer_qty)]

			self._set_serial_batch_bundle_for_disassembly_row(row, serial_nos, batches)

	def _set_serial_batch_for_disassembly_from_available_materials(self):
		available_materials = get_available_materials(self.se_doc.work_order, self.se_doc)
		for row in self.se_doc.items:
			warehouse = row.s_warehouse or row.t_warehouse
			materials = available_materials.get((row.item_code, warehouse))
			if not materials:
				continue

			batches = defaultdict(float)
			serial_nos = []
			qty = row.transfer_qty
			for batch_no, batch_qty in materials.batch_details.items():
				if qty <= 0:
					break

				batch_qty = abs(batch_qty)
				if batch_qty <= qty:
					batches[batch_no] = batch_qty
					qty -= batch_qty
				else:
					batches[batch_no] = qty
					qty = 0

			if materials.serial_nos:
				serial_nos = materials.serial_nos[: int(row.transfer_qty)]

			self._set_serial_batch_bundle_for_disassembly_row(row, serial_nos, batches)

	def _set_serial_batch_bundle_for_disassembly_row(self, row, serial_nos, batches):
		if not serial_nos and not batches:
			return

		warehouse = row.s_warehouse or row.t_warehouse
		bundle_doc = SerialBatchCreation(
			{
				"item_code": row.item_code,
				"warehouse": warehouse,
				"posting_datetime": get_combine_datetime(self.se_doc.posting_date, self.se_doc.posting_time),
				"voucher_type": self.se_doc.doctype,
				"voucher_no": self.se_doc.name,
				"voucher_detail_no": row.name,
				"qty": row.transfer_qty,
				"type_of_transaction": "Inward" if row.t_warehouse else "Outward",
				"company": self.se_doc.company,
				"do_not_submit": True,
			}
		).make_serial_and_batch_bundle(serial_nos=serial_nos, batch_nos=batches)

		row.serial_and_batch_bundle = bundle_doc.name
		row.use_serial_batch_fields = 0

		row.db_set(
			{
				"serial_and_batch_bundle": bundle_doc.name,
				"use_serial_batch_fields": 0,
			}
		)

	def get_items_from_manufacture_stock_entry(self):
		SE = frappe.qb.DocType("Stock Entry")
		SED = frappe.qb.DocType("Stock Entry Detail")
		query = frappe.qb.from_(SED).join(SE).on(SED.parent == SE.name).where(SE.docstatus == 1)

		common_fields = [
			SED.item_code,
			SED.item_name,
			SED.description,
			SED.stock_uom,
			SED.uom,
			SED.basic_rate,
			SED.conversion_factor,
			SED.is_finished_item,
			SED.type,
			SED.is_legacy_scrap_item,
			SED.bom_secondary_item,
			SED.batch_no,
			SED.serial_no,
			SED.use_serial_batch_fields,
			SED.s_warehouse,
			SED.t_warehouse,
			SED.bom_no,
		]

		if self.se_doc.source_stock_entry:
			return (
				query.select(SED.name, SED.qty, SED.transfer_qty, *common_fields)
				.where(SE.name == self.se_doc.source_stock_entry)
				.orderby(SED.idx)
				.run(as_dict=True)
			)

		return (
			query.select(Sum(SED.qty).as_("qty"), Sum(SED.transfer_qty).as_("transfer_qty"), *common_fields)
			.where(SE.purpose == "Manufacture")
			.where(SE.work_order == self.se_doc.work_order)
			.groupby(SED.item_code)
			.orderby(SED.idx)
			.run(as_dict=True)
		)


class StockEntrySABB:
	def __init__(self, se_doc):
		self.se_doc = se_doc

	def make_serial_and_batch_bundle_for_outward(self):
		serial_or_batch_items = get_serial_or_batch_items(self.se_doc.items)
		if not serial_or_batch_items:
			return

		serial_nos, batch_nos = self.get_serial_batch_fields_for_subcontracting_inward()
		already_picked_serial_nos = []

		for row in self.se_doc.items:
			if row.use_serial_batch_fields:
				continue

			if not row.s_warehouse:
				continue

			if row.item_code not in serial_or_batch_items:
				continue

			bundle_doc = None
			if row.serial_and_batch_bundle and abs(row.transfer_qty) != abs(
				frappe.get_cached_value("Serial and Batch Bundle", row.serial_and_batch_bundle, "total_qty")
			):
				bundle_doc = SerialBatchCreation(
					{
						"item_code": row.item_code,
						"warehouse": row.s_warehouse,
						"serial_and_batch_bundle": row.serial_and_batch_bundle,
						"type_of_transaction": "Outward",
						"ignore_serial_nos": already_picked_serial_nos,
						"qty": row.transfer_qty * -1,
					}
				).update_serial_and_batch_entries(
					serial_nos=serial_nos.get(row.name), batch_nos=batch_nos.get(row.name)
				)
			elif not row.serial_and_batch_bundle and frappe.get_single_value(
				"Stock Settings", "auto_create_serial_and_batch_bundle_for_outward"
			):
				bundle_doc = SerialBatchCreation(
					{
						"item_code": row.item_code,
						"warehouse": row.s_warehouse,
						"posting_datetime": get_combine_datetime(
							self.se_doc.posting_date, self.se_doc.posting_time
						),
						"voucher_type": self.se_doc.doctype,
						"voucher_detail_no": row.name,
						"qty": row.transfer_qty * -1,
						"ignore_serial_nos": already_picked_serial_nos,
						"type_of_transaction": "Outward",
						"company": self.se_doc.company,
						"do_not_submit": True,
					}
				).make_serial_and_batch_bundle(
					serial_nos=serial_nos.get(row.name), batch_nos=batch_nos.get(row.name)
				)

			if not bundle_doc:
				continue

			for entry in bundle_doc.entries:
				if not entry.serial_no:
					continue

				already_picked_serial_nos.append(entry.serial_no)

			row.serial_and_batch_bundle = bundle_doc.name

	def get_serial_nos_and_batches_from_sres(self, scio_detail, only_pending=True):
		serial_nos, batch_nos = [], frappe._dict()

		table = frappe.qb.DocType("Stock Reservation Entry")
		child_table = frappe.qb.DocType("Serial and Batch Entry")
		query = (
			frappe.qb.from_(table)
			.join(child_table)
			.on(table.name == child_table.parent)
			.select(child_table.serial_no, child_table.batch_no, child_table.qty)
			.where((table.docstatus == 1) & (table.voucher_detail_no == scio_detail))
		)

		if only_pending:
			query = query.where(child_table.qty != child_table.delivered_qty)
		else:
			query = query.where(child_table.delivered_qty > 0)

		for d in query.run(as_dict=True):
			if d.serial_no and d.serial_no not in serial_nos:
				serial_nos.append(d.serial_no)
			if d.batch_no and d.batch_no not in batch_nos:
				batch_nos[d.batch_no] = d.qty

		return serial_nos, batch_nos

	def get_serial_batch_fields_for_subcontracting_inward(self):
		serial_nos, batch_nos = frappe._dict(), frappe._dict()
		for row in self.se_doc.items:
			if self.se_doc.purpose in [
				"Return Raw Material to Customer",
				"Subcontracting Delivery",
				"Subcontracting Return",
			]:
				if not row.serial_and_batch_bundle:
					serial_nos_list, batch_nos_list = self.get_serial_nos_and_batches_from_sres(
						row.scio_detail, only_pending=self.se_doc.purpose != "Subcontracting Return"
					)

					if len(batch_nos_list) > 1:
						row.use_serial_batch_fields = 0

					if row.use_serial_batch_fields:
						if serial_nos_list and not row.serial_no:
							row.serial_no = "\n".join(serial_nos_list)
						if batch_nos_list and not row.batch_no:
							row.batch_no = next(iter(batch_nos_list.keys()))

					serial_nos[row.name], batch_nos[row.name] = serial_nos_list, batch_nos_list

		return serial_nos, batch_nos

	def get_available_reserved_materials(self):
		from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
			get_reserved_materials,
		)

		voucher_no = self.se_doc.work_order or self.se_doc.subcontracting_order
		reserved_entries = get_reserved_materials(voucher_no)
		if not reserved_entries:
			return {}

		itemwise_serial_batch_qty = frappe._dict()

		for d in reserved_entries:
			key = (d.item_code, d.warehouse)
			if key not in itemwise_serial_batch_qty:
				itemwise_serial_batch_qty[key] = frappe._dict(
					{
						"serial_no": [],
						"batch_no": defaultdict(float),
						"batchwise_sn": defaultdict(list),
					}
				)

			details = itemwise_serial_batch_qty[key]
			if d.batch_no:
				details.batch_no[d.batch_no] += d.qty
				if d.serial_no:
					details.batchwise_sn[d.batch_no].extend(d.serial_no.split("\n"))
			elif d.serial_no:
				details.serial_no.append(d.serial_no)

		return itemwise_serial_batch_qty

	def set_serial_batch_based_on_reservation(self):
		if self.se_doc.work_order and frappe.get_cached_value(
			"Work Order", self.se_doc.work_order, "reserve_stock"
		):
			skip_transfer = frappe.get_cached_value("Work Order", self.se_doc.work_order, "skip_transfer")
			backflush_based_on = get_backflush_based_on(self.se_doc.bom_no)

			if (
				self.se_doc.purpose not in ["Material Transfer for Manufacture"]
				and backflush_based_on != "BOM"
				and not skip_transfer
			):
				return

		reservation_entries = self.get_available_reserved_materials()
		if not reservation_entries:
			return

		new_items_to_add = []
		for d in self.se_doc.items:
			if d.serial_and_batch_bundle or d.serial_no or d.batch_no:
				continue

			key = (d.item_code, d.s_warehouse)
			if details := reservation_entries.get(key):
				original_qty = d.qty
				if batches := details.get("batch_no"):
					for batch_no, qty in batches.items():
						if original_qty <= 0:
							break

						if qty <= 0:
							continue

						if d.batch_no and original_qty > 0:
							new_row = frappe.copy_doc(d)
							new_row.name = None
							new_row.batch_no = batch_no
							new_row.qty = qty
							new_row.idx = d.idx + 1
							if new_row.batch_no and details.get("batchwise_sn"):
								new_row.serial_no = "\n".join(
									details.get("batchwise_sn")[new_row.batch_no][: cint(new_row.qty)]
								)

							new_items_to_add.append(new_row)
							original_qty -= qty
							batches[batch_no] -= qty

						if qty >= d.qty and not d.batch_no:
							d.batch_no = batch_no
							batches[batch_no] -= d.qty
							if d.batch_no and details.get("batchwise_sn"):
								d.serial_no = "\n".join(
									details.get("batchwise_sn")[d.batch_no][: cint(d.qty)]
								)
						elif not d.batch_no:
							d.batch_no = batch_no
							d.qty = qty
							original_qty -= qty
							batches[batch_no] = 0

							if d.batch_no and details.get("batchwise_sn"):
								d.serial_no = "\n".join(
									details.get("batchwise_sn")[d.batch_no][: cint(d.qty)]
								)

				if details.get("serial_no"):
					d.serial_no = "\n".join(details.get("serial_no")[: cint(d.qty)])

				d.use_serial_batch_fields = 1

		for new_row in new_items_to_add:
			self.se_doc.append("items", new_row)

		sorted_items = sorted(self.se_doc.items, key=lambda x: x.item_code)
		if self.se_doc.purpose == "Manufacture":
			# ensure finished item at last
			sorted_items = sorted(sorted_items, key=lambda x: x.t_warehouse)

		idx = 0
		for row in sorted_items:
			idx += 1
			row.idx = idx

		self.se_doc.set("items", sorted_items)


def get_available_materials(work_order, stock_entry_doc=None) -> dict:
	data = get_stock_entry_data(work_order, stock_entry_doc=stock_entry_doc)

	available_materials = {}
	for row in data:
		key = (row.item_code, row.warehouse)
		if row.purpose != "Material Transfer for Manufacture":
			key = (row.item_code, row.s_warehouse)

		if stock_entry_doc and stock_entry_doc.purpose == "Disassemble":
			key = (row.item_code, row.s_warehouse or row.warehouse)

		if key not in available_materials:
			available_materials.setdefault(
				key,
				frappe._dict(
					{"item_details": row, "batch_details": defaultdict(float), "qty": 0, "serial_nos": []}
				),
			)

		item_data = available_materials[key]

		if row.purpose == "Material Transfer for Manufacture" or (
			stock_entry_doc and stock_entry_doc.purpose == "Disassemble" and row.purpose == "Manufacture"
		):
			item_data.qty += row.qty
			if row.batch_no:
				item_data.batch_details[row.batch_no] += row.qty

			elif row.batch_nos:
				for batch_no, qty in row.batch_nos.items():
					item_data.batch_details[batch_no] += qty

			if row.serial_no:
				item_data.serial_nos.extend(get_serial_nos(row.serial_no))
				item_data.serial_nos.sort()

			elif row.serial_nos:
				item_data.serial_nos.extend(get_serial_nos(row.serial_nos))
				item_data.serial_nos.sort()
		else:
			# Consume raw material qty in case of 'Manufacture' or 'Material Consumption for Manufacture'

			item_data.qty -= row.qty
			if row.batch_no:
				item_data.batch_details[row.batch_no] -= row.qty

			elif row.batch_nos:
				for batch_no, qty in row.batch_nos.items():
					item_data.batch_details[batch_no] += qty

			if row.serial_no:
				for serial_no in get_serial_nos(row.serial_no):
					if serial_no in item_data.serial_nos:
						item_data.serial_nos.remove(serial_no)

			elif row.serial_nos:
				for serial_no in get_serial_nos(row.serial_nos):
					if serial_no in item_data.serial_nos:
						item_data.serial_nos.remove(serial_no)

	return available_materials


def get_stock_entry_data(work_order, stock_entry_doc=None):
	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
		get_voucher_wise_serial_batch_from_bundle,
	)

	stock_entry = frappe.qb.DocType("Stock Entry")
	stock_entry_detail = frappe.qb.DocType("Stock Entry Detail")

	data = (
		frappe.qb.from_(stock_entry)
		.from_(stock_entry_detail)
		.select(
			stock_entry_detail.item_name,
			stock_entry_detail.original_item,
			stock_entry_detail.item_code,
			stock_entry_detail.qty,
			(stock_entry_detail.t_warehouse).as_("warehouse"),
			(stock_entry_detail.s_warehouse).as_("s_warehouse"),
			stock_entry_detail.description,
			stock_entry_detail.stock_uom,
			stock_entry_detail.expense_account,
			stock_entry_detail.cost_center,
			stock_entry_detail.serial_and_batch_bundle,
			stock_entry_detail.batch_no,
			stock_entry_detail.serial_no,
			stock_entry.purpose,
			stock_entry.name,
		)
		.where(
			(stock_entry.name == stock_entry_detail.parent)
			& (stock_entry.work_order == work_order)
			& (stock_entry.docstatus == 1)
		)
		.orderby(stock_entry.creation, stock_entry_detail.item_code, stock_entry_detail.idx)
	)

	if stock_entry_doc and stock_entry_doc.purpose == "Disassemble":
		data = data.where(
			stock_entry.purpose.isin(
				[
					"Disassemble",
					"Manufacture",
				]
			)
		)

		data = data.where(stock_entry.name != stock_entry_doc.name)
	else:
		data = data.where(
			stock_entry.purpose.isin(
				[
					"Manufacture",
					"Material Consumption for Manufacture",
					"Material Transfer for Manufacture",
				]
			)
		)

		data = data.where(stock_entry_detail.s_warehouse.isnotnull())

	data = data.run(as_dict=1)

	if not data:
		return []

	voucher_nos = [row.get("name") for row in data if row.get("name")]
	if voucher_nos:
		bundle_data = get_voucher_wise_serial_batch_from_bundle(voucher_no=voucher_nos)
		for row in data:
			key = (row.item_code, row.warehouse, row.name)
			if row.purpose != "Material Transfer for Manufacture":
				key = (row.item_code, row.s_warehouse, row.name)

			if stock_entry_doc and stock_entry_doc.purpose == "Disassemble":
				key = (row.item_code, row.s_warehouse or row.warehouse, row.name)

			if bundle_data.get(key):
				row.update(bundle_data.get(key))

	return data


def create_serial_and_batch_bundle(parent_doc, row, child, type_of_transaction=None):
	item_details = frappe.get_cached_value(
		"Item", child.item_code, ["has_serial_no", "has_batch_no"], as_dict=1
	)

	if not (item_details.has_serial_no or item_details.has_batch_no):
		return

	if not type_of_transaction:
		type_of_transaction = "Inward"

	doc = frappe.get_doc(
		{
			"doctype": "Serial and Batch Bundle",
			"voucher_type": "Stock Entry",
			"item_code": child.item_code,
			"warehouse": child.warehouse,
			"type_of_transaction": type_of_transaction,
			"posting_date": parent_doc.posting_date,
			"posting_time": parent_doc.posting_time,
		}
	)

	precision = frappe.get_precision("Stock Entry Detail", "qty")
	if row.serial_nos and row.batches_to_be_consume:
		doc.has_serial_no = 1
		doc.has_batch_no = 1
		batchwise_serial_nos = get_batchwise_serial_nos(child.item_code, row)
		for batch_no, qty in row.batches_to_be_consume.items():
			while flt(qty, precision) > 0:
				qty -= 1
				doc.append(
					"entries",
					{
						"batch_no": batch_no,
						"serial_no": batchwise_serial_nos.get(batch_no).pop(0),
						"warehouse": row.warehouse,
						"qty": -1,
					},
				)

	elif row.serial_nos:
		doc.has_serial_no = 1
		for serial_no in row.serial_nos:
			doc.append("entries", {"serial_no": serial_no, "warehouse": row.warehouse, "qty": -1})

	elif row.batches_to_be_consume:
		precision = frappe.get_precision("Serial and Batch Entry", "qty")
		doc.has_batch_no = 1
		for batch_no, qty in row.batches_to_be_consume.items():
			if flt(qty, precision) > 0:
				qty = flt(qty, precision)
				doc.append("entries", {"batch_no": batch_no, "warehouse": row.warehouse, "qty": qty * -1})

	if not doc.entries:
		return None

	return doc.insert(ignore_permissions=True).name


def get_batchwise_serial_nos(item_code, row):
	batchwise_serial_nos = {}

	for batch_no in row.batches_to_be_consume:
		serial_nos = frappe.get_all(
			"Serial No",
			filters={"item_code": item_code, "batch_no": batch_no, "name": ("in", row.serial_nos)},
		)

		if serial_nos:
			batchwise_serial_nos[batch_no] = sorted([serial_no.name for serial_no in serial_nos])

	return batchwise_serial_nos


@frappe.whitelist()
def get_expired_batch_items():
	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_auto_batch_nos

	expired_batches = get_expired_batches()
	if not expired_batches:
		return []

	expired_batches_stock = get_auto_batch_nos(
		frappe._dict(
			{
				"batch_no": list(expired_batches.keys()),
				"for_stock_levels": True,
			}
		)
	)

	for row in expired_batches_stock:
		row.update(expired_batches.get(row.batch_no))

	return expired_batches_stock


def get_expired_batches():
	batch = frappe.qb.DocType("Batch")

	data = (
		frappe.qb.from_(batch)
		.select(batch.item, batch.name.as_("batch_no"), batch.stock_uom)
		.where((batch.expiry_date <= nowdate()) & (batch.expiry_date.isnotnull()))
	).run(as_dict=True)

	if not data:
		return []

	expired_batches = frappe._dict()
	for row in data:
		expired_batches[row.batch_no] = row

	return expired_batches


@frappe.whitelist()
def move_sample_to_retention_warehouse(company: str, items: str | list):
	if isinstance(items, str):
		items = json.loads(items)

	retention_warehouse = frappe.get_single_value("Stock Settings", "sample_retention_warehouse")
	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.company = company
	stock_entry.purpose = "Material Transfer"
	stock_entry.set_stock_entry_type()
	for item in items:
		if item.get("sample_quantity") and item.get("serial_and_batch_bundle"):
			warehouse = item.get("t_warehouse") or item.get("warehouse")
			total_qty = 0
			cls_obj = SerialBatchCreation(
				{
					"type_of_transaction": "Outward",
					"serial_and_batch_bundle": item.get("serial_and_batch_bundle"),
					"item_code": item.get("item_code"),
					"warehouse": warehouse,
					"do_not_save": True,
				}
			)
			sabb = cls_obj.duplicate_package()
			batches = get_batch_nos(item.get("serial_and_batch_bundle"))
			sabe_list = []
			for batch_no in batches.keys():
				sample_quantity = validate_sample_quantity(
					item.get("item_code"),
					item.get("sample_quantity"),
					item.get("transfer_qty") or item.get("qty"),
					batch_no,
				)

				sabe = next(item for item in sabb.entries if item.batch_no == batch_no)
				if sample_quantity:
					if sabb.has_serial_no:
						new_sabe = [
							entry
							for entry in sabb.entries
							if entry.batch_no == batch_no
							and frappe.db.exists(
								"Serial No", {"name": entry.serial_no, "warehouse": warehouse}
							)
						][: int(sample_quantity)]
						sabe_list.extend(new_sabe)
						total_qty += len(new_sabe)
					else:
						total_qty += sample_quantity
						sabe.qty = sample_quantity
				else:
					sabb.entries.remove(sabe)

			if total_qty:
				if sabe_list:
					sabb.entries = sabe_list
				sabb.save()

				stock_entry.append(
					"items",
					{
						"item_code": item.get("item_code"),
						"s_warehouse": warehouse,
						"t_warehouse": retention_warehouse,
						"qty": total_qty,
						"basic_rate": item.get("valuation_rate"),
						"uom": item.get("uom"),
						"stock_uom": item.get("stock_uom"),
						"conversion_factor": item.get("conversion_factor") or 1.0,
						"serial_and_batch_bundle": sabb.name,
					},
				)
	if stock_entry.get("items"):
		return stock_entry.as_dict()


@frappe.whitelist()
def validate_sample_quantity(item_code: str, sample_quantity: int, qty: float, batch_no: str | None = None):
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	if cint(qty) < cint(sample_quantity):
		frappe.throw(
			_("Sample quantity {0} cannot be more than received quantity {1}").format(sample_quantity, qty)
		)
	retention_warehouse = frappe.get_single_value("Stock Settings", "sample_retention_warehouse")
	retainted_qty = 0
	if batch_no:
		retainted_qty = get_batch_qty(batch_no, retention_warehouse, item_code)
	max_retain_qty = frappe.get_value("Item", item_code, "sample_quantity")
	if retainted_qty >= max_retain_qty:
		frappe.msgprint(
			_(
				"Maximum Samples - {0} have already been retained for Batch {1} and Item {2} in Batch {3}."
			).format(retainted_qty, batch_no, item_code, batch_no),
			alert=True,
		)
		sample_quantity = 0
	qty_diff = max_retain_qty - retainted_qty
	if cint(sample_quantity) > cint(qty_diff):
		frappe.msgprint(
			_("Maximum Samples - {0} can be retained for Batch {1} and Item {2}.").format(
				max_retain_qty, batch_no, item_code
			),
			alert=True,
		)
		sample_quantity = qty_diff
	return sample_quantity
