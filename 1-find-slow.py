#! /usr/bin/env python2

# Copyright 2014-2015 The Alive authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.



import argparse, glob, re, sys
from language import *
from parser import parse_llvm, parse_opt_file
from multiprocessing import Process
from gen import generate_switched_suite
import signal
import stopit
import pdb
import time
import csv
def block_model(s, sneg, m):
  # First simplify the model.
  sneg.push()
  bools = []
  exprs = []
  req   = []
  skip_model = get_pick_one_type()

  for n in m.decls():
    b = FreshBool()
    name = str(n)
    expr = (Int(name) == m[n])
    sneg.add(b == expr)
    if name in skip_model:
      req += [b]
    else:
      bools += [b]
      exprs += [expr]

  req_exprs = []
  for i in range(len(bools)):
    if sneg.check(req + bools[i+1:]) != unsat:
      req += [bools[i]]
      req_exprs += [exprs[i]]
  assert sneg.check(req) == unsat
  sneg.pop()

  # Now block the simplified model.
  s.add(Not(mk_and(req_exprs)))


def pick_pre_types(s, s2):
  m = s.model()
  skip_model = get_pick_one_type()
  vars = []

  for n in m.decls():
    name = str(n)
    # FIXME: only fix size_* variables?
    if name in skip_model and name.startswith('size_'):
      vars += [Int(name)]
    else:
      s2.add(Int(name) == m[n])

  for v in vars:
    b = FreshBool()
    e = v >= 32
    s2.add(b == e)
    if s2.check(b) == sat:
      s.add(e)
  res = s.check()
  assert res == sat


pre_tactic = AndThen(
  Tactic('propagate-values'),
  Repeat(AndThen(Tactic('simplify'), Tactic('ctx-solver-simplify')))
)
def simplify_pre(f):
  # TODO: extract set of implied things (iffs, tgt=0, etc).
  return pre_tactic.apply(f)[0].as_expr()


def z3_solver_to_smtlib(s):
  a = s.assertions()
  size = len(a) - 1
  _a = (Ast * size)()
  for k in range(size):
    _a[k] = a[k].as_ast()

  return Z3_benchmark_to_smtlib_string(a[size].ctx_ref(), None, None, None, '',
                                       size, _a,  a[size].as_ast())


def gen_benchmark(s):
  if not os.path.isdir('bench'):
    return

  header = ("(set-info :source |\n Generated by Alive 0.1\n"
            " More info in N. P. Lopes, D. Menendez, S. Nagarakatte, J. Regehr."
            "\n Provably Correct Peephole Optimizations with Alive. In PLDI'15."
            "\n|)\n\n")
  string = header + z3_solver_to_smtlib(s)

  files = glob.glob('bench/*.smt2')
  if len(files) == 0:
    filename = 0
  else:
    files.sort(reverse=True)
    filename = int(re.search('(\d+)\.smt2', files[0]).group(1)) + 1
  filename = 'bench/%03d.smt2' % filename
  fd = open(filename, 'w')
  fd.write(string)
  fd.close()


def check_incomplete_solver(res, s):
  if res == unknown:
    print('\nWARNING: The SMT solver gave up. Verification incomplete.')
    print('Solver says: ' + s.reason_unknown())
    exit(-1)


tactic = AndThen(
  Repeat(AndThen(Tactic('simplify'), Tactic('propagate-values'))),
  #Tactic('ctx-simplify'),
  #Tactic('elim-term-ite'),
  #Tactic('simplify'),
  #Tactic('propagate-values'),
  Tactic('solve-eqs'),
  Cond(Probe('is-qfbv'), Tactic('qfbv'), Tactic('bv'))
)

correct_exprs = {}
def check_expr(qvars, expr, error):
  expr = mk_forall(qvars, mk_and(expr))
  id = expr.get_id()
  if id in correct_exprs:
    return
  correct_exprs[id] = expr

  s = tactic.solver()
  s.add(expr)

  if __debug__:
    gen_benchmark(s)

  res = s.check()
  if res != unsat:
    check_incomplete_solver(res, s)
    e, src, tgt, stop, srcv, tgtv, types = error(s)
    print('\nERROR: %s' % e)
    print('Example:')
    print_var_vals(s, srcv, tgtv, stop, types)
    print('Source value: ' + src)
    print('Target value: ' + tgt)
    exit(-1)


def var_type(var, types):
  t = types[Int('t_' + var)].as_long()
  if t == Type.Int:
    return 'i%s' % types[Int('size_' + var)]
  if t == Type.Ptr:
    return var_type('*' + var, types) + '*'
  if t == Type.Array:
    elems = types[Int('val_%s_%s' % (var, 'elems'))]
    return '[%s x %s]' % (elems, var_type('[' + var + ']', types))
  assert False


def val2binhex(v, bits):
  return '0x%0*X' % ((bits+3) // 4, v)
  #if bits % 4 == 0:
  #  return '0x%0*X' % (bits / 4, v)
  #return format(v, '#0'+str(bits)+'b')


def str_model(s, v):
  val = s.model().evaluate(v, True)
  if isinstance(val, BoolRef):
    return "true" if is_true(val) else "false"
  valu = val.as_long()
  vals = val.as_signed_long()
  bin = val2binhex(valu, val.size())
  if valu != vals:
    return "%s (%d, %d)" % (bin, valu, vals)
  return "%s (%d)" % (bin, valu)


def _print_var_vals(s, vars, stopv, seen, types):
  for k,v in vars.items():
    if k == stopv:
      return
    if k in seen:
      continue
    seen |= set([k])
    print("%s %s = %s" % (k, var_type(k, types), str_model(s, v[0])))


def print_var_vals(s, vs1, vs2, stopv, types):
  seen = set()
  _print_var_vals(s, vs1, stopv, seen, types)
  _print_var_vals(s, vs2, stopv, seen, types)


def get_smt_vars(f):
  if is_const(f):
    if is_bv_value(f) or is_bool(f):
      return {}
    return {str(f): f}

  ret = {}
  if isinstance(f, list):
    for v in f:
      ret.update(get_smt_vars(v))
    return ret

  for c in f.children():
    ret.update(get_smt_vars(c))
  return ret


def check_refinement(srcv, tgtv, types, extra_cnstrs, users):
  for k,v in srcv.items():
    # skip instructions only on one side; assumes they remain unchanged
    if k[0] == 'C' or k not in tgtv:
      continue

    (a, defa, poisona, qvars) = v
    (b, defb, poisonb, qvarsb) = tgtv[k]
    defb = mk_and(defb)
    poisonb = mk_and(poisonb)

    n_users = users[k]
    base_cnstr = defa + poisona + extra_cnstrs + n_users

    # Check if domain of defined values of Src implies that of Tgt.
    check_expr(qvars, base_cnstr + [mk_not(defb)], lambda s :
      ("Domain of definedness of Target is smaller than Source's for %s %s\n"
         % (var_type(k, types), k),
       str_model(s, a), 'undef', k, srcv, tgtv, types))

    # Check if domain of poison values of Src implies that of Tgt.
    check_expr(qvars, base_cnstr + [mk_not(poisonb)], lambda s :
      ("Domain of poisoness of Target is smaller than Source's for %s %s\n"
         % (var_type(k, types), k),
       str_model(s, a), 'poison', k, srcv, tgtv, types))

    # Check that final values of vars are equal.
    check_expr(qvars, base_cnstr + [a != b], lambda s :
      ("Mismatch in values of %s %s\n" % (var_type(k, types), k),
       str_model(s, a), str_model(s, b), k, srcv, tgtv, types))


def infer_flags(srcv, tgtv, types, extra_cnstrs, prev_flags, users):
  query = []
  flag_vars_src = {}
  flag_vars_tgt = {}

  for k,v in srcv.items():
    # skip instructions only on one side; assumes they remain unchanged
    if k[0] == 'C' or k not in tgtv:
      continue

    (a, defa, poisona, qvars) = v
    (b, defb, poisonb, qvarsb) = tgtv[k]

    pre = mk_and(defa + poisona + prev_flags + extra_cnstrs)
    eq = [] if a.eq(b) else [a == b]
    q = mk_implies(pre, mk_and(defb + poisonb + eq))
    if is_true(q):
      continue
    q = mk_and(users[k] + [q])

    input_vars = []
    for k,v in get_smt_vars(q).items():
      if k[0] == '%' or k[0] == 'C' or k.startswith('icmp_') or\
         k.startswith('alloca') or k.startswith('mem_') or k.startswith('ana_'):
        input_vars.append(v)
      elif k.startswith('f_'):
        if k.endswith('_src'):
          flag_vars_src[k] = v
        else:
          assert k.endswith('_tgt')
          flag_vars_tgt[k] = v
      elif k.startswith('u_') or k.startswith('undef'):
        continue
      else:
        print("Unknown smt var: " + str(v))
        exit(-1)

    q = mk_exists(qvars, q)
    q = mk_forall(input_vars, q)
    query.append(q)

  s = Solver()#tactic.solver()
  # s.set("timeout", 2)
  s.add(query)
  if __debug__:
    gen_benchmark(s)

  print("Checking flags...")
  res = s.check()
  if res == unsat:
    # optimization is incorrect. Run the normal procedure for nice diagnostics.
    check_refinement(srcv, tgtv, types, extra_cnstrs, users)
    assert False

  # enumerate all models (all possible flag assignments)
  models = []
  while True:
    check_incomplete_solver(res, s)
    m = s.model()
    min_model = []
    for v in flag_vars_src.values():
      val = m[v]
      if val and val.as_long() == 1:
        min_model.append(v == 1)
    for v in flag_vars_tgt.values():
      val = m[v]
      if val and val.as_long() == 0:
        min_model.append(v == 0)

    m = mk_and(min_model)
    models.append(m)
    s.add(mk_not(m))
    if __debug__:
      gen_benchmark(s)

    res = s.check()
    if res == unsat:
      return mk_or(models)


gbl_prev_flags = []

def check_typed_opt(pre, src, ident_src, tgt, ident_tgt, types, users):
  srcv = toSMT(src, ident_src, True)
  tgtv = toSMT(tgt, ident_tgt, False)
  pre_d, pre = pre.toSMT(srcv)
  extra_cnstrs = pre_d + pre +\
                 srcv.getAllocaConstraints() + tgtv.getAllocaConstraints()

  # 1) check preconditions of BBs
  tgtbbs = tgtv.bb_pres
  for k,v in srcv.bb_pres.items():
    if k not in tgtbbs:
      continue
    # assume open world. May need to add language support to state that a BB is
    # complete (closed world)
    p1 = mk_and(v)
    p2 = mk_and(tgtbbs[k])
    check_expr([], [p1 != p2] + extra_cnstrs, lambda s :
      ("Mismatch in preconditions for BB '%s'\n" % k, str_model(s, p1),
       str_model(s, p2), None, srcv, tgtv, types))

  # 2) check register values
  if do_infer_flags():
    global gbl_prev_flags
    flgs = infer_flags(srcv, tgtv, types, extra_cnstrs, gbl_prev_flags, users)
    gbl_prev_flags = [simplify_pre(mk_and(gbl_prev_flags + [flgs]))]
  else:
    check_refinement(srcv, tgtv, types, extra_cnstrs, users)

  # 3) check that the final memory state is similar in both programs
  idx = BitVec('idx', get_ptr_size())
  val1 = srcv.load(idx)
  val2 = tgtv.load(idx)
  check_expr(srcv.mem_qvars, extra_cnstrs + [val1 != val2], lambda s :
    ('Mismatch in final memory state in ptr %s' % str_model(s, idx),
     str_model(s, val1), str_model(s, val2), None, srcv, tgtv, types))

# @stopit.threading_timeoutable(default='timeout')
def check_opt(opt, timeout, bitwidth, hide_progress):
  name, pre, src, tgt, ident_src, ident_tgt, used_src, used_tgt, skip_tgt = opt

  print('----------------------------------------')
  print('Optimization: ' + name)
  print('Timeout: ' + str(timeout))
  print('Bitwidth: ' + str(bitwidth))
  print('Precondition: ' + str(pre))
  print_prog(src, set([]))
  print('=>')
  print_prog(tgt, skip_tgt)
  print()

  reset_pick_one_type()
  global gbl_prev_flags
  gbl_prev_flags = []

  # infer allowed types for registers
  type_src = getTypeConstraints(ident_src, bitwidth)
  type_tgt = getTypeConstraints(ident_tgt, bitwidth)
  type_pre = pre.getTypeConstraints(bitwidth)

  s = SolverFor('QF_LIA')
  # s.set("timeout", 2)
  s.add(type_pre)
  print("Type checking precondition...")
  if s.check() != sat:
    print('Precondition does not type check')
    exit(-1)

  # Only one type per variable/expression in the precondition is required.
  for v in s.model().decls():
    register_pick_one_type(v)

  s.add(type_src)
  unregister_pick_one_type(get_smt_vars(type_src))
  print("Type checking source...")
  if s.check() != sat:
    print('Source program does not type check')
    exit(-1)

  s.add(type_tgt)
  unregister_pick_one_type(get_smt_vars(type_tgt))
  print("type checking destination...")
  if s.check() != sat:
    print('Source and Target programs do not type check')
    exit(-1)

  # Pointers are assumed to be either 32 or 64 bits
  ptrsize = Int('ptrsize')
  s.add(Or(ptrsize == 32, ptrsize == 64))

  sneg = SolverFor('QF_LIA')
  sneg.add(Not(mk_and([type_pre] + type_src + type_tgt)))

  has_unreach = any(v.startswith('unreachable') for v in ident_tgt.keys())
  for v in ident_src.keys():
    if v[0] == '%' and v not in used_src and v not in used_tgt and\
       v in skip_tgt and not has_unreach:
      print('ERROR: Temporary register %s unused and not overwritten' % v)
      exit(-1)

  for v in ident_tgt.keys():
    if v[0] == '%' and v not in used_tgt and v not in ident_src:
      print('ERROR: Temporary register %s unused and does not overwrite any'\
            ' Source register' % v)
      exit(-1)

  # build constraints that indicate the number of users for each register.
  users_count = countUsers(src)
  users = {}
  for k in ident_src.keys():
    n_users = users_count.get(k)
    users[k] = [get_users_var(k) != n_users] if n_users else []

  # pick one representative type for types in Pre
  res = s.check()
  assert res != unknown
  if res == sat:
    s2 = SolverFor('QF_LIA')
    s2.add(s.assertions())
    pick_pre_types(s, s2)

  # now check for correctness
  proofs = 0
  while res == sat:
    types = s.model()
    set_ptr_size(types)
    fixupTypes(ident_src, types)
    fixupTypes(ident_tgt, types)
    pre.fixupTypes(types)
    check_typed_opt(pre, src, ident_src, tgt, ident_tgt, types, users)
    block_model(s, sneg, types)
    proofs += 1
    if not hide_progress:
      sys.stdout.write('\rDone: ' + str(proofs))
      sys.stdout.flush()
    sys.stdout.write('\rChecking Result')
    sys.stdout.flush()
    # s.set("timeout", 2)
    res = s.check()
    assert res != unknown

  if res == unsat:
    print('\nOptimization is correct!')
    if do_infer_flags():
      print('Flags: %s' % gbl_prev_flags[0])
    print()
  else:
    print('\nVerification incomplete; did not check all bit widths\n')

  return True # succeeded, did not time out

# a row in the CSV data that we write.
class Row:
  def __init__(self, path, name, bitwidth, timeout, time_elapsed, exitcode, did_timeout):
    self.path = path
    self.name = name
    self.bitwidth = bitwidth
    self.timeout = timeout
    self.time_elapsed = time_elapsed
    self.exitcode = exitcode
    self.did_timeout = did_timeout

  @classmethod
  def write_header(cls, csv_writer):
    csv_writer.writerow(["path", "name", "bitwidth", "timeout", "time_elapsed", "exitcode", "did_timeout"])

  def write(self, csv_writer):
    csv_writer.writerow([self.path, self.name, self.bitwidth, self.timeout, self.time_elapsed, self.exitcode, self.did_timeout])


def verify_opt_with_timeout(path, opt, timeout, bitwidth):
  name, pre, src, tgt, ident_src, ident_tgt, used_src, used_tgt, skip_tgt = opt
  print '----------------------------------------'
  if str(pre) != "true": print("%s has precondition; skipping." % (name, )); return None
  # timer using python standard library
  start_time = time.time()
  hide_progress = False
  p1 = Process(target=check_opt, args=(opt, timeout, bitwidth, hide_progress))
  p1.start()
  p1.join(timeout=timeout)
  end_time = time.time()
  p1.terminate()
  exitcode = p1.exitcode
  did_timeout = exitcode is None
  print("timeout: [%4s]. bitwidth: [%4s], elapsed: [%4s] seconds. exitcode: [%s]. Timed out? [%s]" % \
    (timeout, bitwidth, end_time - start_time, exitcode, did_timeout))
  row = Row(path, name, bitwidth, timeout, end_time - start_time, exitcode, did_timeout)
  return row


def verify_all():
  out_path = "experiment-out-data/out.csv"
  paths = ["tests/instcombine/addsub.opt",
           "tests/instcombine/andorxor.opt",
           "tests/instcombine/muldivrem.opt",
           "tests/instcombine/select.opt",
           "tests/instcombine/shift.opt"]
  with open(out_path, "w") as of:
    wof = csv.writer(of, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    Row.write_header(wof)

    # first run everything for 1 minute, then 5 minutes, then 1 hour
    did_timeout_cache = {}
    for timeout in [1, 10, 60, 300, 1800, 3600]:
        for path in paths:
          with open(path, "r") as f:
            opts = parse_opt_file(f.read())
            for opt in opts:
                name = opt[0]
                # 2^10 = 10^3
                # (2^10)^3 = 10^9 = giga bit width
                for bitwidth in [pow(2, i) for i in range(1, 30)]:
                  # only run those that did time out in lower timeouts, or that has never been run before
                  if did_timeout_cache.get((path, name, bitwidth)) in [True, None]: 
                      row = verify_opt_with_timeout(path, opt, timeout, bitwidth)
                      if row is None: continue # skipped because precondition.
                      # if timed out, skip the larger bitwidths
                      did_timeout_cache[(path, name, bitwidth)] = row.did_timeout
                      row.write(wof)
                      of.flush()
                      # if this bitwidth timed out, then ignore larger bitwidths, as they too
                      # will have timed out.
                      if row.did_timeout: break
                  else:
                    print("already succeeded running Path [%s], Name [%s],  bitwidth [%4s]" % \
                            (path, name, bitwidth))

if __name__ == "__main__":
  try:
    verify_all()
  except IOError as e:
    print('ERROR:', e, file=sys.stderr)
    exit(-1)
  except KeyboardInterrupt:
    print('\nCaught Ctrl-C. Exiting..')
