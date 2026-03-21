"""
Build a dependency graph of entities → instruments → priority relationships.

Programmatic first pass:
1. Parse entities (from NER or provided)
2. Parse table rows (from bs4)
3. Match instruments to entities by text proximity
4. Infer priority from XBRL concepts and keywords
5. Render as interactive graph (mermaid.js)

Usage:
    ANTHROPIC_API_KEY=sk-... python3 graph.py path/to/debt_note.html -o graph.html
    python3 graph.py path/to/debt_note.html --entities "Altice USA,CSC Holdings,Lightpath" -o graph.html
"""

import sys
import os
import re
import json
import argparse
from typing import Optional
from ner import html_to_text, extract_entities
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Parse instruments from HTML
# ---------------------------------------------------------------------------

def _preprocess_ixbrl(html: str) -> str:
    """
    Pre-process iXBRL HTML to make it parseable by bs4.
    - Flatten nested ix:nonfraction tags (keep innermost)
    - Remove ix:nonnumeric wrappers (keep their content)
    - Remove ix:continuation tags (keep content)
    """
    # Remove ix:nonnumeric open/close tags but keep content
    html = re.sub(r'<ix:nonnumeric[^>]*>', '', html)
    html = re.sub(r'</ix:nonnumeric>', '', html)
    
    # Remove ix:continuation open/close tags but keep content
    html = re.sub(r'<ix:continuation[^>]*>', '', html)
    html = re.sub(r'</ix:continuation>', '', html)
    
    # Flatten nested ix:nonfraction: when one ix:nonfraction contains another,
    # remove the outer one and keep the inner
    # Pattern: <ix:nonfraction ...><ix:nonfraction ...>VALUE</ix:nonfraction></ix:nonfraction>
    for _ in range(3):  # iterate to handle deep nesting
        html = re.sub(
            r'<ix:nonfraction([^>]*)><ix:nonfraction([^>]*)>([^<]*)</ix:nonfraction></ix:nonfraction>',
            r'<ix:nonfraction\2>\3</ix:nonfraction>',
            html
        )
    
    return html


def _flag_duplicate_amounts(instruments: list[dict]) -> list[dict]:
    """
    Flag instruments that share the same amount — potential double-counting.
    Don't resolve, just flag for LLM to decide.
    """
    from collections import defaultdict
    by_amt = defaultdict(list)
    for inst in instruments:
        if inst.get('amount_mm') and inst['amount_mm'] > 0:
            key = round(inst['amount_mm'], 1)
            by_amt[key].append(inst)
    
    for amt, group in by_amt.items():
        if len(group) > 1:
            labels = [g['label'][:50] for g in group]
            for inst in group:
                others = [l for l in labels if l != inst['label'][:50]]
                inst['duplicate_flag'] = f"Same amount (${amt}mm) as: {'; '.join(others)}"
    
    # Also check sums: if any pair of instruments sums to another instrument's amount
    all_amts = [(inst, round(inst['amount_mm'], 1)) for inst in instruments if inst.get('amount_mm') and inst['amount_mm'] > 0]
    amt_to_inst = {round(inst['amount_mm'], 1): inst for inst in instruments if inst.get('amount_mm')}
    
    for i, (inst_a, amt_a) in enumerate(all_amts):
        for j, (inst_b, amt_b) in enumerate(all_amts):
            if j <= i:
                continue
            total = round(amt_a + amt_b, 1)
            if total in amt_to_inst:
                inst_total = amt_to_inst[total]
                if inst_total is not inst_a and inst_total is not inst_b:
                    inst_total.setdefault('sum_flag', '')
                    inst_total['sum_flag'] = f"This amount (${total}mm) = {inst_a['label'][:30]} (${amt_a}mm) + {inst_b['label'][:30]} (${amt_b}mm)"
    
    return instruments


def parse_instruments(html: str, entities: list[str] = None) -> list[dict]:
    """
    Extract instruments from HTML tables using section-header walking.
    
    The table has section headers like "CSC Holdings Senior Guaranteed Notes"
    that tell us both the entity and priority for all rows below until next header.
    
    We walk top-to-bottom, tracking current_entity and current_priority.
    """
    html = _preprocess_ixbrl(html)
    soup = BeautifulSoup(html, 'html.parser')
    entities = entities or []
    
    # --- Extract footnote definitions from text AFTER tables ---
    # Remove table content first, then find (1), (2) patterns
    text_after_tables = re.sub(r'<table[^>]*>.*?</table>', ' [TABLE] ', str(soup), flags=re.DOTALL)
    text_after_tables = BeautifulSoup(text_after_tables, 'html.parser').get_text(separator=' ')
    text_after_tables = re.sub(r'\s+', ' ', text_after_tables)
    
    footnotes = {}
    # Numbered footnotes: (1)...(2)...
    for m in re.finditer(r'\((\d{1,2})\)\s*([A-Z].{20,800}?)(?=\(\d{1,2}\)\s*[A-Z]|$)', text_after_tables):
        footnotes[m.group(1)] = re.sub(r'\s+', ' ', m.group(2)).strip()
    # Letter footnotes: (a)...(b)...
    for m in re.finditer(r'\(([a-z])\)\s*([A-Z].{20,800}?)(?=\([a-z]\)\s*[A-Z]|$)', text_after_tables):
        footnotes[m.group(1)] = re.sub(r'\s+', ' ', m.group(2)).strip()
    
    # Build flexible entity patterns (longest first)
    entity_patterns = []
    for entity in sorted(entities, key=len, reverse=True):
        entity_patterns.append((entity, re.compile(re.escape(entity), re.IGNORECASE)))
        # Also try distinctive words
        for word in entity.split():
            if len(word) >= 4 and word.lower() not in ('inc.', 'inc', 'llc', 'corp', 'corp.', 'corporation', 'company', 'the'):
                entity_patterns.append((entity, re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)))
    
    def _strip_suffix(name: str) -> str:
        return re.sub(r',?\s*(Inc\.?|LLC|Ltd\.?|Corp\.?|Corporation|Company|Co\.|L\.P\.)$', '', name, flags=re.IGNORECASE).strip()
    
    def match_entity(text: str) -> Optional[str]:
        """Find the most specific entity match in text (longest full-name match wins)."""
        best = None
        best_len = 0
        # First pass: try full entity names and suffix-stripped versions
        for entity in sorted(entities, key=len, reverse=True):
            for variant in [entity, _strip_suffix(entity)]:
                if len(variant) < 4:
                    continue
                if re.search(re.escape(variant), text, re.IGNORECASE):
                    if len(variant) > best_len:
                        best = entity  # return the original entity name
                        best_len = len(variant)
        if best:
            return best
        # Second pass: try distinctive word patterns (only if word is unique to one entity)
        for entity, pat in entity_patterns:
            if pat.search(text):
                word = pat.pattern.replace(r'\b', '').replace('\\', '')
                matches_count = sum(1 for e in entities if word.lower() in e.lower())
                if matches_count == 1:
                    return entity
        return None
    
    def infer_priority_from_header(text: str) -> str:
        t = text.lower()
        # Check most specific first
        if 'senior secured' in t or 'first lien' in t or 'second lien' in t:
            return 'Senior Secured'
        elif 'senior priority guaranteed' in t:
            return 'Senior Priority Guaranteed'
        elif 'priority guaranteed' in t:
            return 'Priority Guaranteed'
        elif 'senior guaranteed' in t:
            return 'Guaranteed'
        elif 'guaranteed' in t and 'note' in t:
            return 'Guaranteed'
        elif 'subordinated' in t:
            return 'Subordinated'
        elif 'unsecured' in t:
            return 'Unsecured'
        elif 'senior note' in t:
            return 'Unsecured'
        # Broader: just "secured" without "unsecured"
        elif 'secured' in t and 'unsecured' not in t:
            return 'Senior Secured'
        return None
    
    instruments = []
    current_entity = None
    current_priority_hint = None
    
    for tidx, table in enumerate(soup.find_all('table')):
        for ridx, tr in enumerate(table.find_all('tr')):
            all_ix = []
            for cell in tr.find_all(['td', 'th']):
                for ix in cell.find_all('ix:nonfraction'):
                    name = ix.get('name', '')
                    raw = ix.get_text(strip=True)
                    scale = int(ix.get('scale', '0'))
                    unit = ix.get('unitref', '')
                    
                    val_mm = None
                    if raw and raw not in ('—', '\u2014', 'no', ''):
                        try:
                            val_mm = round(float(raw.replace(',', '')) * (10**scale) / 1e6, 3)
                        except:
                            pass
                    elif raw in ('—', '\u2014'):
                        val_mm = 0.0
                    
                    all_ix.append({
                        'concept': name.split(':')[-1] if ':' in name else name,
                        'ctx': ix.get('contextref', ''),
                        'unit': unit,
                        'value_mm': val_mm,
                        'raw': raw,
                    })
            
            first_cell = tr.find(['td', 'th'])
            label = first_cell.get_text(separator=' ', strip=True) if first_cell else ''
            
            usd_ix = [ix for ix in all_ix if ix['unit'] == 'usd']
            rate_ix = [ix for ix in all_ix if ix['unit'] not in ('usd', '')
                       and ('Rate' in ix['concept'] or 'Percentage' in ix['concept'])]
            
            # --- Is this a section header? (no USD amounts, has text) ---
            # Two patterns exist:
            #   Pattern A: "CSC Holdings Senior Guaranteed Notes" → entity + priority
            #   Pattern B: "Senior Secured First Lien" → priority only (entity in data rows)
            if not usd_ix and label:
                entity_match = match_entity(label)
                priority_match = infer_priority_from_header(label)
                
                if entity_match:
                    current_entity = entity_match
                if priority_match:
                    current_priority_hint = priority_match
                
                # If we found either, this is a header row
                if entity_match or priority_match:
                    continue
                
                # Even if no entity/priority detected, skip empty rows
                continue
            
            if not usd_ix:
                continue
            
            # --- This is a data row ---
            concepts = {ix['concept'] for ix in all_ix}
            label_lower = label.lower()
            
            # Check if this row itself contains an entity name (overrides current)
            row_entity = match_entity(label) or current_entity
            
            # Infer type
            inst_type = 'unknown'
            if any(c in concepts for c in ('LineOfCredit', 'LineOfCreditFacilityInterestRateAtPeriodEnd')):
                if 'revolv' in label_lower:
                    inst_type = 'revolver'
                elif 'term loan' in label_lower:
                    inst_type = 'term_loan'
                else:
                    inst_type = 'credit_facility'
            elif 'SeniorNotes' in concepts or 'senior note' in label_lower:
                inst_type = 'senior_notes'
            elif 'FinanceLeaseLiability' in concepts or 'finance lease' in label_lower:
                inst_type = 'finance_lease'
            elif 'OperatingLeaseLiability' in concepts or 'operating lease' in label_lower:
                inst_type = 'operating_lease'
            elif any('Maturities' in c or 'Repayments' in c for c in concepts):
                inst_type = 'maturity_schedule'
            elif any(c in concepts for c in ('LongTermDebtCurrent', 'LongTermDebtNoncurrent',
                                              'LongTermDebtFairValue', 'DebtLongtermAndShorttermCombinedAmount')):
                inst_type = 'total_or_subtotal'
            elif any(c in concepts for c in ('GainsLossesOnExtinguishmentOfDebt', 'RepaymentsOfDebt', 'ProceedsFromLoans')):
                inst_type = 'transaction'
            elif 'DebtInstrumentFaceAmount' in concepts or 'LongTermDebt' in concepts:
                inst_type = 'debt_instrument'
            
            # Infer priority: from the row itself first, then from section header hint
            priority = None
            if 'senior secured' in label_lower or inst_type in ('revolver', 'term_loan', 'credit_facility'):
                priority = 'Senior Secured'
            elif 'senior guaranteed' in label_lower or 'guaranteed note' in label_lower:
                priority = 'Guaranteed'
            elif 'subordinated' in label_lower:
                priority = 'Subordinated'
            elif 'senior note' in label_lower or 'senior unsecured' in label_lower:
                priority = 'Unsecured'
            elif inst_type in ('finance_lease', 'operating_lease'):
                priority = 'Senior Secured'
            
            # Fall back to section header hint
            if not priority and current_priority_hint:
                priority = current_priority_hint
            
            # Default: notes without explicit priority → Unsecured
            if not priority:
                if inst_type in ('senior_notes', 'debt_instrument'):
                    priority = 'Unsecured'
                else:
                    priority = 'unknown'
            
            # Pick amount: ordered preference — NET concepts first, then carrying, then face
            # LongTermDebt/SeniorNotes/LineOfCredit = net of discounts/issuance costs
            # DebtInstrumentCarryingAmount = often means principal (confusing XBRL naming)
            # DebtInstrumentFaceAmount = always face/principal
            net_concepts_ordered = [
                'LongTermDebt', 'SeniorNotes', 'LineOfCredit',
                'FinanceLeaseLiability', 'OperatingLeaseLiability',
                'FinanceLeaseLiabilityCurrent', 'FinanceLeaseLiabilityNoncurrent',
                'OperatingLeaseLiabilityCurrent', 'OperatingLeaseLiabilityNoncurrent',
                'ShortTermBorrowings',
                'DebtInstrumentCarryingAmount',  # lower priority — often means principal
            ]
            
            amount = None
            amount_concept = None
            for preferred in net_concepts_ordered:
                for ix in usd_ix:
                    if ix['concept'] == preferred:
                        amount = ix['value_mm']
                        amount_concept = ix['concept']
                        break
                if amount is not None:
                    break
            if amount is None and usd_ix:
                amount = usd_ix[0]['value_mm']
                amount_concept = usd_ix[0]['concept']
            
            # Extract available/unused capacity if present
            amount_available = None
            for ix in usd_ix:
                if ix['concept'] in ('LineOfCreditFacilityRemainingBorrowingCapacity',):
                    amount_available = ix['value_mm']
                    break
            
            rate = rate_ix[0]['raw'] if rate_ix else None
            
            # Build all_amounts for LLM visibility (include context for period disambiguation)
            all_amounts = [{'concept': ix['concept'], 'value_mm': ix['value_mm'], 'ctx': ix.get('ctx', '')} 
                          for ix in usd_ix if ix['value_mm'] is not None]
            
            # Extract footnote references from label: (1), (2), (a), (b), etc.
            fn_refs = re.findall(r'\((\d{1,2})\)', label) + re.findall(r'\(([a-z])\)', label)
            resolved_fns = [f"({r}): {footnotes[r][:300]}" for r in fn_refs if r in footnotes]
            
            instruments.append({
                'id': f"t{tidx}_r{ridx}",
                'label': label[:150],
                'amount_mm': amount,
                'amount_concept': amount_concept,
                'amount_available_mm': amount_available,
                'all_amounts': all_amounts,
                'rate': rate,
                'type': inst_type,
                'priority': priority,
                'entity': row_entity,
                'concepts': list(concepts),
                'footnotes': resolved_fns,
                'source': f"debt_note table {tidx} row {ridx}",
            })
    
    # Flag potential duplicates for LLM
    instruments = _flag_duplicate_amounts(instruments)
    
    # Assign unmatched instruments to parent entity (the filer).
    parent = None
    if entities:
        for e in entities:
            if any(s in e.lower() for s in ('inc', 'corporation', 'corp', 'company', 'co.')):
                parent = e
                break
        if not parent:
            parent = entities[0]
    
    if parent:
        for inst in instruments:
            if inst['entity'] is None:
                inst['entity'] = parent
    
    return instruments


# ---------------------------------------------------------------------------
# Lease + Balance Sheet parsing
# ---------------------------------------------------------------------------

def parse_leases(html: str, parent_entity: str) -> list[dict]:
    """
    Extract lease liabilities from the lease note HTML.
    All leases → parent entity, priority = Senior Secured.
    
    Strategy: look for current+noncurrent split first, fall back to totals.
    """
    html = _preprocess_ixbrl(html)
    soup = BeautifulSoup(html, 'html.parser')
    
    # Collect ALL lease-related USD ix values
    all_lease_ix = {}  # concept -> value_mm (first occurrence = current period)
    for ix in soup.find_all('ix:nonfraction'):
        name = ix.get('name', '').split(':')[-1]
        if 'Lease' not in name and 'lease' not in name:
            continue
        unit = ix.get('unitref', '')
        if 'usd' not in unit.lower() and 'USD' not in unit:
            continue
        raw = ix.get_text(strip=True)
        scale = int(ix.get('scale', '0'))
        
        val_mm = None
        if raw and raw not in ('—', '\u2014'):
            try:
                val_mm = round(float(raw.replace(',', '')) * (10**scale) / 1e6, 3)
            except:
                pass
        
        if val_mm is not None and name not in all_lease_ix:
            all_lease_ix[name] = val_mm
    
    print(f"  Lease concepts found: {list(all_lease_ix.keys())}", file=sys.stderr)
    
    leases = []
    
    # --- Finance leases ---
    if 'FinanceLeaseLiabilityCurrent' in all_lease_ix or 'FinanceLeaseLiabilityNoncurrent' in all_lease_ix:
        # Split available
        if 'FinanceLeaseLiabilityCurrent' in all_lease_ix:
            leases.append({
                'id': 'lease_FinanceLeaseLiabilityCurrent',
                'label': 'Finance Lease Obligations - Current',
                'amount_mm': all_lease_ix['FinanceLeaseLiabilityCurrent'],
                'rate': None, 'type': 'finance_lease', 'priority': 'Senior Secured',
                'entity': parent_entity, 'concepts': ['FinanceLeaseLiabilityCurrent'], 'footnotes': [], 'source': 'lease_note',
            })
        if 'FinanceLeaseLiabilityNoncurrent' in all_lease_ix:
            leases.append({
                'id': 'lease_FinanceLeaseLiabilityNoncurrent',
                'label': 'Finance Lease Obligations - Non-Current',
                'amount_mm': all_lease_ix['FinanceLeaseLiabilityNoncurrent'],
                'rate': None, 'type': 'finance_lease', 'priority': 'Senior Secured',
                'entity': parent_entity, 'concepts': ['FinanceLeaseLiabilityNoncurrent'], 'footnotes': [], 'source': 'lease_note',
            })
    elif 'FinanceLeaseLiability' in all_lease_ix:
        # Only total available
        leases.append({
            'id': 'lease_FinanceLeaseLiability',
            'label': 'Finance Lease Obligations',
            'amount_mm': all_lease_ix['FinanceLeaseLiability'],
            'rate': None, 'type': 'finance_lease', 'priority': 'Senior Secured',
            'entity': parent_entity, 'concepts': ['FinanceLeaseLiability'], 'footnotes': [], 'source': 'lease_note',
        })
    
    # --- Operating leases ---
    if 'OperatingLeaseLiabilityCurrent' in all_lease_ix or 'OperatingLeaseLiabilityNoncurrent' in all_lease_ix:
        if 'OperatingLeaseLiabilityCurrent' in all_lease_ix:
            leases.append({
                'id': 'lease_OperatingLeaseLiabilityCurrent',
                'label': 'Operating Lease Liabilities - Current',
                'amount_mm': all_lease_ix['OperatingLeaseLiabilityCurrent'],
                'rate': None, 'type': 'operating_lease', 'priority': 'Senior Secured',
                'entity': parent_entity, 'concepts': ['OperatingLeaseLiabilityCurrent'], 'footnotes': [], 'source': 'lease_note',
            })
        if 'OperatingLeaseLiabilityNoncurrent' in all_lease_ix:
            leases.append({
                'id': 'lease_OperatingLeaseLiabilityNoncurrent',
                'label': 'Operating Lease Liabilities - Non-Current',
                'amount_mm': all_lease_ix['OperatingLeaseLiabilityNoncurrent'],
                'rate': None, 'type': 'operating_lease', 'priority': 'Senior Secured',
                'entity': parent_entity, 'concepts': ['OperatingLeaseLiabilityNoncurrent'], 'footnotes': [], 'source': 'lease_note',
            })
    elif 'OperatingLeaseLiability' in all_lease_ix:
        # Only total — use it
        leases.append({
            'id': 'lease_OperatingLeaseLiability',
            'label': 'Operating Lease Liabilities',
            'amount_mm': all_lease_ix['OperatingLeaseLiability'],
            'rate': None, 'type': 'operating_lease', 'priority': 'Senior Secured',
            'entity': parent_entity, 'concepts': ['OperatingLeaseLiability'], 'footnotes': [], 'source': 'lease_note',
        })
    
    return leases


def parse_balance_sheet(bs_json: dict) -> dict:
    """Extract cash and NCI from balance sheet JSON."""
    period = bs_json['columns'][0]['key']
    result = {'cash_mm': 0, 'nci_mm': 0}
    
    for row in bs_json['rows']:
        concept = row.get('concept', '')
        vals = row.get('values', {})
        if period not in vals:
            continue
        n = vals[period].get('numeric_value')
        if n is None:
            continue
        v = round(n / 1e6, 3)
        
        if 'CashAndCashEquivalent' in concept:
            result['cash_mm'] = v
        elif concept in ('MinorityInterest', 'RedeemableNoncontrollingInterest',
                         'NoncontrollingInterest'):
            result['nci_mm'] = v
    
    return result


def deduplicate_leases(debt_instruments: list[dict], lease_instruments: list[dict]) -> list[dict]:
    """
    Only add leases from lease note if not already in debt table.
    Checks:
    1. Exact amount match (single lease amount = debt table amount)
    2. Sum match (current + noncurrent from lease sheet = single "Other" in debt table)
    3. Individual match (e.g. finance lease current from lease = finance lease current in debt)
    """
    # Collect all amounts from debt table (especially lease-related and "Other" rows)
    debt_amounts = set()
    for inst in debt_instruments:
        if inst.get('amount_mm'):
            debt_amounts.add(round(inst['amount_mm'], 1))
    
    # Check lease sum against debt amounts
    # Group lease items by type (finance vs operating)
    finance_leases = [l for l in lease_instruments if 'Finance' in l.get('concepts', [''])[0]]
    operating_leases = [l for l in lease_instruments if 'Operating' in l.get('concepts', [''])[0]]
    
    finance_sum = round(sum(l['amount_mm'] for l in finance_leases if l['amount_mm']), 1)
    operating_sum = round(sum(l['amount_mm'] for l in operating_leases if l['amount_mm']), 1)
    
    # Check if the sums match any debt table amount
    finance_sum_matched = finance_sum in debt_amounts and finance_sum > 0
    operating_sum_matched = operating_sum in debt_amounts and operating_sum > 0
    
    if finance_sum_matched:
        print(f"  Dedup: finance lease sum ${finance_sum}mm matches debt table — skipping", file=sys.stderr)
    if operating_sum_matched:
        print(f"  Dedup: operating lease sum ${operating_sum}mm matches debt table — skipping", file=sys.stderr)
    
    new_leases = []
    for lease in lease_instruments:
        amt = round(lease['amount_mm'], 1) if lease['amount_mm'] else 0
        concept = lease.get('concepts', [''])[0]
        
        # Skip if exact amount match
        if amt in debt_amounts and amt > 0:
            print(f"  Dedup: {lease['label']} ${amt}mm exact match — skipping", file=sys.stderr)
            continue
        
        # Skip if this is a finance lease and the sum was matched
        if 'Finance' in concept and finance_sum_matched:
            continue
        
        # Skip if this is an operating lease and the sum was matched
        if 'Operating' in concept and operating_sum_matched:
            continue
        
        new_leases.append(lease)
    
    return new_leases


# ---------------------------------------------------------------------------
# Match instruments to entities
# ---------------------------------------------------------------------------

def _build_entity_patterns(entities: list[str]) -> list[tuple[str, re.Pattern]]:
    """
    For each entity, build regex patterns that match flexible variations.
    e.g. "Cablevision Lightpath" also generates a pattern for just "Lightpath"
    """
    patterns = []
    for entity in entities:
        # Full name
        patterns.append((entity, re.compile(re.escape(entity), re.IGNORECASE)))
        # Also try each word that's 4+ chars (catches "Lightpath" from "Cablevision Lightpath")
        words = [w for w in entity.split() if len(w) >= 4 
                 and w.lower() not in ('inc.', 'inc', 'llc', 'corp', 'corp.', 'corporation', 'holdings', 'company', 'the')]
        for word in words:
            patterns.append((entity, re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)))
    return patterns


def match_entities_to_instruments(entities: list[str], instruments: list[dict], 
                                   narrative: str) -> dict[str, list[str]]:
    """
    For each entity, find which instruments belong to it.
    Uses: label matching, narrative section matching, table position.
    """
    entities_sorted = sorted(entities, key=len, reverse=True)
    entity_patterns = _build_entity_patterns(entities_sorted)
    
    entity_instruments = {e: [] for e in entities}
    
    # Build a map of narrative sections: find where each entity is discussed
    # by looking for section headers or repeated mentions
    entity_sections = {}  # entity -> list of (start, end) positions
    for entity in entities_sorted:
        positions = [m.start() for m in re.finditer(re.escape(entity), narrative, re.IGNORECASE)]
        # Also check short names
        words = [w for w in entity.split() if len(w) >= 5 
                 and w.lower() not in ('inc.', 'inc', 'llc', 'corp', 'corp.', 'corporation', 'holdings', 'company')]
        for word in words:
            positions.extend(m.start() for m in re.finditer(r'\b' + re.escape(word) + r'\b', narrative, re.IGNORECASE))
        entity_sections[entity] = sorted(set(positions))
    
    for inst in instruments:
        label = inst['label']
        matched_entity = None
        
        # Strategy 1: entity name (or key word) in instrument label
        for entity, pattern in entity_patterns:
            if pattern.search(label):
                matched_entity = entity
                break
        
        # Strategy 2: narrative proximity — find which entity section 
        # mentions this instrument's rate or key terms
        if not matched_entity and inst['type'] not in ('maturity_schedule', 'total_or_subtotal', 'transaction'):
            if inst['rate']:
                key_word = inst['rate'] + '%'
                best_entity = None
                best_dist = float('inf')
                
                for entity in entities_sorted:
                    for pos in entity_sections.get(entity, []):
                        chunk = narrative[pos:pos+800]
                        if key_word in chunk:
                            # Distance from entity mention to rate mention
                            rate_pos = chunk.index(key_word)
                            if rate_pos < best_dist:
                                best_dist = rate_pos
                                best_entity = entity
                
                if best_entity:
                    matched_entity = best_entity
        
        inst['entity'] = matched_entity
        if matched_entity:
            entity_instruments[matched_entity].append(inst['id'])
    
    # Fallback: if only one entity, assign all unmatched instruments to it
    if len(entities) == 1:
        sole_entity = entities[0]
        for inst in instruments:
            if inst['entity'] is None:
                inst['entity'] = sole_entity
                entity_instruments[sole_entity].append(inst['id'])
    
    return entity_instruments


# ---------------------------------------------------------------------------
# Build and render graph
# ---------------------------------------------------------------------------

def build_mermaid(entities: list[str], instruments: list[dict], 
                  entity_instruments: dict[str, list[str]]) -> str:
    """Build a mermaid.js graph definition."""
    lines = ["graph LR"]
    
    inst_by_id = {i['id']: i for i in instruments}
    
    # Entity nodes
    for entity in entities:
        safe_id = re.sub(r'[^a-zA-Z0-9]', '_', entity)
        lines.append(f'    {safe_id}["{entity}"]')
    
    # Group instruments by entity and priority
    for entity in entities:
        safe_entity = re.sub(r'[^a-zA-Z0-9]', '_', entity)
        inst_ids = entity_instruments.get(entity, [])
        
        by_priority = {}
        for iid in inst_ids:
            inst = inst_by_id.get(iid)
            if not inst:
                continue
            pri = inst['priority']
            by_priority.setdefault(pri, []).append(inst)
        
        for pri, insts in sorted(by_priority.items(), key=lambda x: {
            'Senior Secured': 0, 'Guaranteed': 1, 'Unsecured': 2, 'Subordinated': 3
        }.get(x[0], 9)):
            pri_id = f"{safe_entity}_{re.sub(r'[^a-zA-Z0-9]', '_', pri)}"
            lines.append(f'    {pri_id}("{pri}")')
            lines.append(f'    {safe_entity} --> {pri_id}')
            
            for inst in insts:
                inst_id = re.sub(r'[^a-zA-Z0-9]', '_', inst['id'])
                amt = f"${inst['amount_mm']:,.1f}mm" if inst['amount_mm'] else "$?mm"
                rate_str = f" {inst['rate']}%" if inst['rate'] else ""
                short_label = inst['label'][:50].replace('"', "'")
                lines.append(f'    {inst_id}["{short_label}<br/>{amt}{rate_str}"]')
                lines.append(f'    {pri_id} --> {inst_id}')
    
    # Unmatched instruments
    unmatched = [i for i in instruments if i['entity'] is None]
    if unmatched:
        lines.append(f'    UNMATCHED["⚠ Unmatched"]')
        for inst in unmatched:
            inst_id = re.sub(r'[^a-zA-Z0-9]', '_', inst['id'])
            amt = f"${inst['amount_mm']:,.1f}mm" if inst['amount_mm'] else "$?mm"
            short_label = inst['label'][:50].replace('"', "'")
            lines.append(f'    {inst_id}["{short_label}<br/>{amt}"]')
            lines.append(f'    UNMATCHED --> {inst_id}')
    
    return "\n".join(lines)


def render_graph_html(mermaid_code: str, entities: list[str], instruments: list[dict],
                      entity_instruments: dict[str, list[str]], filepath: str,
                      llm_corrections: dict = None) -> str:
    """Render the graph + a summary table as HTML."""
    esc = lambda s: str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    inst_by_id = {i['id']: i for i in instruments}
    
    # Build corrections lookup for display
    corr_by_id = {}
    if llm_corrections:
        for c in llm_corrections.get('corrections', []):
            corr_by_id[c['id']] = c
    
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Capital Structure Graph: {esc(os.path.basename(filepath))}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
body {{ font-family: -apple-system, sans-serif; font-size: 13px; margin: 20px; }}
h1 {{ font-size: 18px; }}
h2 {{ font-size: 15px; margin-top: 30px; }}
.mermaid {{ margin: 20px 0; min-height: 600px; overflow: auto; }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; font-size: 12px; }}
th {{ background: #f0f0f0; }}
.amt {{ text-align: right; font-family: monospace; }}
.type {{ font-size: 11px; color: #666; }}
.skip {{ opacity: 0.4; }}
.reason {{ font-size: 11px; color: #0066cc; font-style: italic; }}
.corrected {{ background: #fffde7; }}
.excluded {{ background: #ffebee; text-decoration: line-through; opacity: 0.6; }}
</style>
</head><body>
<h1>Capital Structure Graph</h1>
"""
    
    if llm_corrections and llm_corrections.get('approach'):
        html += f'<p style="background:#e3f2fd;padding:8px;border-left:3px solid #1976d2;margin:10px 0;font-size:12px"><strong>LLM Approach:</strong> {esc(llm_corrections["approach"])}</p>\n'
    
    html += f"""
<div class="mermaid">
{mermaid_code}
</div>

<h2>Summary Table</h2>
"""
    
    for entity in entities:
        inst_ids = entity_instruments.get(entity, [])
        all_insts = [inst_by_id[iid] for iid in inst_ids if iid in inst_by_id]
        total = sum(i['amount_mm'] or 0 for i in all_insts)
        
        html += f'<h3>{esc(entity)} ({len(all_insts)} rows, ${total:,.1f}mm)</h3>\n'
        html += '<table><tr><th>Instrument</th><th>Amount ($mm)</th><th>Available ($mm)</th><th>Rate</th><th>Priority</th><th>Conf</th><th>Source</th><th>LLM Reason</th></tr>\n'
        for inst in all_insts:
            amt = f"{inst['amount_mm']:,.3f}" if inst['amount_mm'] is not None else ''
            avail = f"{inst['amount_available_mm']:,.3f}" if inst.get('amount_available_mm') else ''
            name = inst.get('clean_name') or inst['label'][:80]
            reason = inst.get('_reason', '')
            corr = corr_by_id.get(inst['id'])
            confidence = ''
            if corr and corr.get('confidence') is not None:
                c = corr['confidence']
                color = '#4caf50' if c >= 0.8 else '#ff9800' if c >= 0.5 else '#f44336'
                confidence = f'<span style="color:{color}">{c:.0%}</span>'
            source = inst.get('source', '')
            row_class = ' class="corrected"' if corr else ''
            html += f'<tr{row_class}><td>{esc(name)}</td><td class="amt">{amt}</td><td class="amt">{avail}</td><td>{esc(inst.get("rate","") or "")}</td><td>{esc(inst["priority"])}</td><td>{confidence}</td><td class="type">{esc(source)}</td><td class="reason">{esc(reason)}</td></tr>\n'
        html += '</table>\n'
    
    # Excluded by LLM
    excluded = [i for i in instruments if i.get('_excluded')]
    if excluded:
        html += f'<h3 style="color:#c00">❌ Excluded by LLM ({len(excluded)})</h3>\n'
        html += '<table><tr><th>Instrument</th><th>Amount ($mm)</th><th>Type</th><th>Reason</th></tr>\n'
        for inst in excluded:
            amt = f"{inst['amount_mm']:,.3f}" if inst['amount_mm'] is not None else ''
            reason = inst.get('_reason', '')
            html += f'<tr class="excluded"><td>{esc(inst["label"][:80])}</td><td class="amt">{amt}</td><td class="type">{esc(inst["type"])}</td><td class="reason">{esc(reason)}</td></tr>\n'
        html += '</table>\n'
    
    # Guarantor relationships
    if llm_corrections and llm_corrections.get('guarantor_relationships'):
        grels = llm_corrections['guarantor_relationships']
        html += f'<h2>Guarantor Relationships ({len(grels)})</h2>\n'
        html += '<table><tr><th>Instrument/Group</th><th>Issuer</th><th>Guarantors</th><th>Type</th><th>Source</th></tr>\n'
        for g in grels:
            guarantors = ', '.join(g.get('guarantors', []))
            html += f'<tr><td>{esc(g.get("instrument_or_group",""))}</td><td>{esc(g.get("issuer",""))}</td><td>{esc(guarantors)}</td><td>{esc(g.get("guarantee_type",""))}</td><td class="reason">{esc(g.get("narrative_quote",""))}</td></tr>\n'
        html += '</table>\n'
    
    # LLM corrections summary
    if corr_by_id:
        html += f'<h2>LLM Corrections Log ({len(corr_by_id)} items)</h2>\n'
        html += '<table><tr><th>ID</th><th>Changes</th><th>Reason</th></tr>\n'
        for cid, c in corr_by_id.items():
            changes = []
            if c.get('entity'): changes.append(f"entity → {c['entity']}")
            if c.get('priority'): changes.append(f"priority → {c['priority']}")
            if c.get('exclude'): changes.append("EXCLUDED")
            if c.get('clean_name'): changes.append(f"name → {c['clean_name'][:40]}")
            if c.get('issue_date'): changes.append(f"issued {c['issue_date']}")
            if c.get('amount_available_mm') is not None: changes.append(f"avail ${c['amount_available_mm']}mm")
            reason = c.get('reason', '')
            html += f'<tr><td>{esc(cid)}</td><td>{esc(", ".join(changes))}</td><td class="reason">{esc(reason)}</td></tr>\n'
        html += '</table>\n'
    
    html += '<script>mermaid.initialize({startOnLoad:true, theme:"default", flowchart:{useMaxWidth:false, htmlLabels:true}, securityLevel:"loose"});</script>\n'
    html += '</body></html>'
    return html


# ---------------------------------------------------------------------------
# LLM Validation
# ---------------------------------------------------------------------------

LLM_VALIDATE_SYSTEM = """You are a financial analyst reviewing a programmatic extraction of debt instruments from an SEC 10-K filing.

You receive:
1. A list of entities (companies/subsidiaries) mentioned in this filing
2. A list of instruments extracted from the debt table, all initially assigned to the parent company (the filer)
3. The narrative text from the debt footnote
4. The target annual_period year

CRITICAL RULES for amounts:
- **Use only the target year column** (provided as annual_period). Tables have multiple years — ignore prior year columns.
- **Always prefer NET amounts over PRINCIPAL/FACE amounts**. Tables often have sub-columns like "Principal Amount" and "Net of Premiums, Discounts and Issuance Costs" — always use the NET column. This is the carrying amount. The programmatic extraction tries to pick the right one via XBRL concepts (LongTermDebt, SeniorNotes = carrying; DebtInstrumentFaceAmount = face), but verify via the `amount_concept` and `all_amounts` fields.
- Each instrument's `all_amounts` shows every USD value in that row with its XBRL concept. If you see both a face amount and carrying amount, the carrying amount is correct.

Your job:

## 1. Entity/Issuer assignment
All instruments start assigned to the parent. Reassign to a subsidiary ONLY when:
- The instrument label contains a subsidiary name or abbreviation (e.g. "B+L" = Bausch + Lomb)
- The narrative explicitly says a subsidiary issued/borrowed it
- **CHECK the narrative_context field** on each instrument — if it says "BHA issued" or "[Subsidiary] issued", reassign to that subsidiary
- "the Company issued" = parent company (no reassignment needed)
- If unsure, leave it with the parent

## 2. Guarantor relationships  
Parse the narrative for guarantor language. Look for:
- "guaranteed by substantially all domestic subsidiaries" → parent issues, subsidiaries guarantee
- "guaranteed on a senior secured basis by..." → tells you priority AND guarantors
- "the obligations are guaranteed by each restricted subsidiary of [Entity]"
This determines the hierarchy: issuer → guarantors → priority level

## 3. Priority verification
- Credit facilities/term loans → Senior Secured
- "First Lien" → Senior Secured  
- "Second Lien" → Senior Secured (lower seniority within secured)
- "Senior Guaranteed Notes" (where "Guaranteed" is IN THE NAME of the instrument) → Guaranteed
- "Senior Notes" → Unsecured. THIS IS THE DEFAULT. Even if the narrative says "guaranteed by subsidiaries" or "guaranteed on a senior unsecured basis", that describes the guarantor structure, NOT the priority. The priority is Unsecured unless the instrument name itself says "Guaranteed Notes" or "Secured Notes".
- "Senior Secured Notes" → Senior Secured
- Leases → Senior Secured
- IMPORTANT: Do NOT confuse guarantor language with priority. "Fully and unconditionally guaranteed by subsidiaries" = tells you WHO guarantees, not the priority tier. Priority comes from the instrument name.

## 4. Exclude non-instruments
Mark as exclude: totals, subtotals, current portions, maturity schedules, fair values, gains/losses, balance roll-forwards.

## 5. Duplicate detection
Some instruments have `duplicate_flag` or `sum_flag` fields indicating potential double-counting. Investigate these:
- If two rows have the same amount, check footnotes to determine if one is a component of the other
- If a row's amount = sum of two other rows, it's likely a total → exclude it
- For cross-sheet duplicates (debt table "Other" includes finance leases from lease sheet), prefer the granular breakdown

## 6. Output table structure awareness
The final output table has these columns:
  Instrument Name | Amount Outstanding ($mm) | Amount Available ($mm) | Coupon (%) | Maturity | Priority | Parent Issuer | Issue Date

Key rules:
- Amount Outstanding = the drawn/outstanding balance
- Amount Available = remaining borrowing capacity (for revolvers/credit facilities ONLY)
- If "Unused lines of credit" appears as a separate row, its amount should become the amount_available_mm on the corresponding credit facility row, not a separate instrument
- Lines of credit rows should be mapped: used amount → Amount Outstanding, unused → Amount Available, total → EXCLUDE

## 7. Supplement from narrative
Add: issue_date, amount_available_mm (revolvers), coupon, maturity_year, clean_name.

Return ONLY valid JSON:
{
  "approach": "Brief 2-3 sentence summary of what you see: how many instruments, which entities, what period, which column you're using (net vs principal), any notable issues.",
  "corrections": [
    {
      "id": "instrument id",
      "entity": "corrected entity or null if unchanged",
      "priority": "corrected priority or null if unchanged",
      "exclude": true/false,
      "amount_mm": "corrected amount if the programmatic pick was wrong (e.g. picked principal instead of net, or wrong year). null if unchanged.",
      "issue_date": "YYYY-MM-DD or null",
      "amount_available_mm": number or null,
      "coupon": "rate or null",
      "maturity_year": "YYYY or null",
      "clean_name": "display name or null",
      "confidence": 0.0 to 1.0,
      "reason": "WHY you changed this. For entity: 'B+L prefix = Bausch + Lomb'. For amount: 'picked net over principal'. For exclude: 'subtotal row'."
    }
  ],
  "guarantor_relationships": [
    {
      "instrument_or_group": "description",
      "issuer": "entity that issued",
      "guarantors": ["list of guarantor entities"],
      "guarantee_type": "senior secured / senior unsecured / etc",
      "narrative_quote": "short key phrase from the text"
    }
  ],
  "company_name": "parent company name"
}

Only include instruments in corrections where you have a change or supplementary info."""


def _find_narrative_snippet(instrument: dict, narrative: str) -> str:
    """Find the narrative paragraph that discusses this instrument."""
    rate = instrument.get('rate')
    label = instrument.get('label', '')
    
    # Split narrative into paragraphs
    paragraphs = re.split(r'\n\s*\n', narrative)
    
    # Strategy 1: match by rate (most reliable for notes)
    if rate:
        rate_pattern = rate.replace('.', r'\.') + '%'
        for para in paragraphs:
            if re.search(rate_pattern, para) and len(para) > 50:
                return para.strip()[:500]
    
    # Strategy 2: match by key words from label
    label_words = [w for w in label.split() if len(w) >= 5 and not w.replace(',', '').replace('.', '').isdigit()]
    if label_words:
        for para in paragraphs:
            matches = sum(1 for w in label_words if w.lower() in para.lower())
            if matches >= 2 and len(para) > 50:
                return para.strip()[:500]
    
    return ''


def llm_validate(instruments: list[dict], entities: list[str], 
                  narrative: str, api_key: str, annual_period: int = 2024) -> dict:
    """Call LLM to validate/correct programmatic extraction."""
    import httpx
    
    # Build instrument summary with matched narrative snippets
    inst_summary = []
    for inst in instruments:
        snippet = _find_narrative_snippet(inst, narrative)
        entry = {
            'id': inst['id'],
            'label': inst['label'],
            'amount_mm': inst['amount_mm'],
            'amount_concept': inst.get('amount_concept'),
            'amount_available_mm': inst.get('amount_available_mm'),
            'all_amounts': inst.get('all_amounts', []),
            'rate': inst.get('rate'),
            'type': inst['type'],
            'priority': inst['priority'],
            'entity': inst.get('entity'),
            'source': inst.get('source', ''),
        }
        if snippet:
            entry['narrative_context'] = snippet
        if inst.get('footnotes'):
            entry['footnotes'] = inst['footnotes']
        if inst.get('duplicate_flag'):
            entry['duplicate_flag'] = inst['duplicate_flag']
        if inst.get('sum_flag'):
            entry['sum_flag'] = inst['sum_flag']
        inst_summary.append(entry)
    
    user_msg = json.dumps({
        'annual_period': annual_period,
        'entities': entities,
        'instruments': inst_summary,
        'narrative': narrative[:15000],
    }, indent=2, default=str)
    
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8192,
            "temperature": 0,
            "system": LLM_VALIDATE_SYSTEM,
            "messages": [{"role": "user", "content": user_msg}],
        },
        timeout=180.0,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    return _extract_json(text)


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "```" in text:
        for part in text.split("```")[1::2]:
            cleaned = part.strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            try:
                return json.loads(cleaned)
            except:
                continue
    fb = text.find("{")
    lb = text.rfind("}")
    if fb != -1 and lb > fb:
        try:
            return json.loads(text[fb:lb+1])
        except:
            pass
    raise ValueError(f"Could not parse JSON from LLM: {text[:300]}")


def apply_corrections(instruments: list[dict], corrections: dict) -> list[dict]:
    """Apply LLM corrections to instruments. Returns filtered list."""
    corrections_by_id = {c['id']: c for c in corrections.get('corrections', [])}
    
    result = []
    for inst in instruments:
        corr = corrections_by_id.get(inst['id'])
        if corr:
            # Exclude?
            if corr.get('exclude'):
                inst['_excluded'] = True
                inst['_reason'] = corr.get('reason', 'LLM excluded')
                result.append(inst)
                continue
            
            # Apply corrections
            if corr.get('entity'):
                inst['entity'] = corr['entity']
            if corr.get('priority'):
                inst['priority'] = corr['priority']
            if corr.get('amount_mm') is not None:
                inst['amount_mm'] = corr['amount_mm']
            if corr.get('issue_date'):
                inst['issue_date'] = corr['issue_date']
            if corr.get('amount_available_mm') is not None:
                inst['amount_available_mm'] = corr['amount_available_mm']
            if corr.get('coupon'):
                inst['coupon'] = corr['coupon']
            if corr.get('maturity_year'):
                inst['maturity_year'] = corr['maturity_year']
            if corr.get('clean_name'):
                inst['clean_name'] = corr['clean_name']
            
            inst['_reason'] = corr.get('reason', '')
            inst['_confidence'] = corr.get('confidence')
        
        result.append(inst)
    
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input_dir', help='Directory containing debt_note.html, lease_note.html, balance_sheet.json')
    parser.add_argument('--market-cap', type=float, default=0)
    parser.add_argument('-o', '--output', default=None)
    parser.add_argument('--entities', default=None, help='Comma-separated entity names (skip NER)')
    args = parser.parse_args()
    
    # Fuzzy find files in directory
    import glob
    d = args.input_dir.rstrip('/')
    
    def find_file(patterns):
        for pat in patterns:
            matches = glob.glob(os.path.join(d, pat))
            if matches:
                return matches[0]
            # Also try case-insensitive
            for f in os.listdir(d):
                if any(p.replace('*', '') in f.lower() for p in patterns):
                    return os.path.join(d, f)
        return None
    
    debt_file = find_file(['*debt*note*.html', '*debt*.html', '*debt*'])
    lease_file = find_file(['*lease*note*.html', '*lease*.html', '*lease*'])
    bs_file = find_file(['*balance*sheet*.json', '*balance*.json', '*bs*.json'])
    meta_file = find_file(['*metadata*.json', '*meta*.json'])
    
    if not debt_file:
        print(f"No debt note found in {d}", file=sys.stderr)
        sys.exit(1)
    
    # Read metadata for annual_period
    annual_period = 2024  # default
    if meta_file:
        with open(meta_file) as f:
            meta = json.load(f)
        annual_period = meta.get('annual_period', 2024)
    
    print(f"Files: debt={os.path.basename(debt_file)}", file=sys.stderr, end='')
    if lease_file: print(f", lease={os.path.basename(lease_file)}", file=sys.stderr, end='')
    if bs_file: print(f", bs={os.path.basename(bs_file)}", file=sys.stderr, end='')
    if meta_file: print(f", meta={os.path.basename(meta_file)}", file=sys.stderr, end='')
    print(f" | period={annual_period}", file=sys.stderr)
    
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.entities:
        print("ANTHROPIC_API_KEY required (or provide --entities)", file=sys.stderr)
        sys.exit(1)
    
    with open(debt_file) as f:
        debt_html = f.read()
    
    text = html_to_text(debt_html)
    print(f"Text: {len(text)} chars", file=sys.stderr)
    
    # Get entities
    if args.entities:
        entities = [e.strip() for e in args.entities.split(',')]
    else:
        print("Extracting entities...", file=sys.stderr)
        entities = extract_entities(text, api_key)
    
    # Dedup (keep longer names, remove substrings)
    entities = sorted(entities, key=len, reverse=True)
    filtered = []
    for e in entities:
        if not any(e.lower() in kept.lower() for kept in filtered):
            filtered.append(e)
    entities = filtered
    print(f"Entities: {entities}", file=sys.stderr)
    
    # Determine parent entity
    parent = None
    for e in entities:
        if any(s in e.lower() for s in ('inc', 'corporation', 'corp', 'company', 'co.')):
            parent = e
            break
    if not parent and entities:
        parent = entities[0]
    
    # Parse debt instruments
    instruments = parse_instruments(debt_html, entities)
    print(f"Debt instruments: {len(instruments)} rows", file=sys.stderr)
    
    # Parse leases
    if lease_file:
        with open(lease_file) as f:
            lease_html = f.read()
        lease_instruments = parse_leases(lease_html, parent or 'Unknown')
        new_leases = deduplicate_leases(instruments, lease_instruments)
        print(f"Leases: {len(lease_instruments)} found, {len(new_leases)} new (not in debt table)", file=sys.stderr)
        instruments.extend(new_leases)
    
    # Parse balance sheet
    bs_data = {'cash_mm': 0, 'nci_mm': 0}
    if bs_file:
        with open(bs_file) as f:
            bs_json = json.load(f)
        bs_data = parse_balance_sheet(bs_json)
        print(f"Balance sheet: cash=${bs_data['cash_mm']:,.1f}mm, NCI=${bs_data['nci_mm']:,.1f}mm", file=sys.stderr)
    
    print(f"Total rows: {len(instruments)}", file=sys.stderr)
    
    # LLM validation + guarantor parsing
    llm_corrections = None
    if api_key:
        print("LLM validating...", file=sys.stderr)
        try:
            llm_corrections = llm_validate(instruments, entities, text, api_key, annual_period)
            n_corr = len(llm_corrections.get('corrections', []))
            n_guar = len(llm_corrections.get('guarantor_relationships', []))
            
            approach = llm_corrections.get('approach', '')
            if approach:
                print(f"  LLM approach: {approach}", file=sys.stderr)
            
            print(f"  {n_corr} corrections, {n_guar} guarantor relationships", file=sys.stderr)
            
            for c in llm_corrections.get('corrections', []):
                changes = []
                if c.get('entity'): changes.append(f"entity→{c['entity']}")
                if c.get('priority'): changes.append(f"priority→{c['priority']}")
                if c.get('amount_mm') is not None: changes.append(f"amt→${c['amount_mm']}mm")
                if c.get('exclude'): changes.append("EXCLUDE")
                if c.get('clean_name'): changes.append(f"name→{c['clean_name'][:30]}")
                if c.get('amount_available_mm') is not None: changes.append(f"avail→${c['amount_available_mm']}mm")
                conf = f" [{c.get('confidence','')}]" if c.get('confidence') is not None else ''
                print(f"  [{c['id']}] {', '.join(changes)}{conf} | {c.get('reason','')}", file=sys.stderr)
            
            for g in llm_corrections.get('guarantor_relationships', []):
                print(f"  guarantor: {g.get('issuer','')} → {g.get('guarantors',[])} ({g.get('guarantee_type','')})", file=sys.stderr)
            
            instruments = apply_corrections(instruments, llm_corrections)
        except Exception as e:
            print(f"  LLM failed: {e}", file=sys.stderr)
    
    # Build entity_instruments map
    entity_instruments = {e: [] for e in entities}
    for inst in instruments:
        if inst.get('_excluded'):
            continue
        e = inst.get('entity')
        if e and e in entity_instruments:
            entity_instruments[e].append(inst['id'])
    
    for e, ids in entity_instruments.items():
        print(f"  {e}: {len(ids)} instruments", file=sys.stderr)
    
    # Build graph + render
    active = [i for i in instruments if not i.get('_excluded')]
    mermaid = build_mermaid(entities, active, entity_instruments)
    output = render_graph_html(mermaid, entities, active, entity_instruments,
                                debt_file, llm_corrections=llm_corrections)
    
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
