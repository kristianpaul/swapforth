import sys
import struct
from functools import partial
import operator
import array
import copy
import time
import re

import swapforth

CMASK = (2 ** 32) - 1       # Cell mask

def u32(x):
    return x & CMASK

def w32(x):
    if -0x80000000 <= x < 0x80000000:
        return x
    else:
        x &= CMASK
        if x & 0x80000000:
            x -= 0x100000000
        return x

def truth(pred):
    return [0, -1][pred]

def setimmediate(func):
    func.is_immediate = True
    return func

def ba(x):
    return array.array('B', x)

class ForthException(Exception):
    def __init__(self, value):
        self.value = value

class SwapForth:

    def __init__(self):
        self.d = []                 # data stack
        self.r = []                 # return stack
        self.dict = {}              # the dictionary
        self.xts = []               # execution token (xt) table
        self.ip = 0                 # instruction pointer for inner interpreter
        self.loopC = 0              # loop count
        self.loopL = 0              # loop limit
        self.leaves = []            # tracking LEAVEs from DO..LOOP
        self.ram = array.array('B') # memory
        self.out = sys.stdout.write # default console output

        def allot(n, d):
            r = partial(self.lit, len(self.ram))
            r.__doc__ = d
            self.ram.extend([0] * n)
            return r

        self.tib = allot(256, "TIB")
        self.sourcea = allot(4, "SOURCEA")
        self.sourcec = allot(4, "SOURCEC")
        self.to_in = allot(4, ">IN")
        self.base = allot(4, "BASE")
        self.state = allot(4, "STATE")

        # Run through own bound methods, adding each to the dict
        isforth = re.compile(r"[A-Z0-9<>=\-\[\],@!:;+?/*]+$")
        for name in dir(self):
            o = getattr(self, name)
            if not isforth.match(name) and o.__doc__:
                # name was not a valid Forth name; try start of the docstring
                name = o.__doc__.split()[0]
            if callable(o) and isforth.match(name):
                self.dict[name] = o

        self.DECIMAL()

    def lit(self, n):
        """ push literal N on the stack """
        self.d.append(n)

    def popn(self, n):
        r = self.d[-n:]
        self.d = self.d[:-n]
        return r

    def q(self, s):
        for w in s.split():
            if w in self.dict:
                self.dict[w]()
            else:
                self.lit(int(w))

    def binary(self, op):
        b = self.d.pop()
        self.d[-1] = w32(op(self.d[-1], b))

    def dpop(self):
        r = self.d.pop() << 32
        r += self.d.pop() & (2 ** 32 - 1)
        return r

    def dlit(self, d):
        self.lit(w32(d & (2 ** 32 - 1)))
        self.lit(w32(d >> 32))

    def pops(self):
        n = self.d.pop()
        a = self.d.pop()
        return self.ram[a:a+n].tostring()

    # Start of Forth words
    #
    # If the word is a legal Python identifier, then
    # use that name. Otherwise (e.g. '+') the Forth name is in
    # the docstring.

    def HERE(self):
        self.lit(len(self.ram))

    def THROW(self):
        e = self.d.pop()
        if e:
            raise ForthException(e)

    def CATCH(self):
        self.q('SOURCEA @ SOURCEC @ >IN @')
        source_spec = self.popn(3)
        (ds,rs,ip) = (len(self.d) - 1, len(self.r), self.ip)
        try:
            self.EXECUTE()
        except ForthException as e:
            if len(self.d) > ds:
                self.d = self.d[:ds]
            else:
                self.d = self.d + [0] * (ds - len(self.d))
            self.r = self.r[:rs]
            self.ip = ip
            self.lit(source_spec[0])
            self.lit(source_spec[1])
            self.lit(source_spec[2])
            self.q('>IN ! SOURCEC ! SOURCEA !')
            self.lit(e.value)
        else:
            self.lit(0)

    def DEPTH(self):
        self.lit(len(self.d))

    def SOURCE(self):
        self.sourcea()
        self.fetch()
        self.sourcec()
        self.fetch()

    def fetch(self):
        """ @ """
        a = self.d.pop()
        self.lit(*struct.unpack('i', self.ram[a:a + 4]))

    def c_fetch(self):
        """ C@ """
        a = self.d.pop()
        self.lit(self.ram[a])

    def store(self):
        """ ! """
        a = self.d.pop()
        x = self.d.pop()
        self.ram[a:a + 4] = array.array('B', struct.pack('i', x))

    def c_store(self):
        """ C! """
        a = self.d.pop()
        x = self.d.pop()
        self.ram[a] = x & 0xff

    def comma(self):
        """ , """
        self.ram.extend(ba(struct.pack('i', self.d.pop())))

    def c_comma(self):
        """ C, """
        self.ram.extend([self.d.pop()])

    def slash_string(self):
        """ /STRING """
        n = self.d.pop()
        self.d[-2] += n
        self.d[-1] -= n

    def PARSE(self):
        delim = self.d.pop()
        self.q('SOURCE >IN @ /STRING')

        self.q('OVER >R')
        while True:
            if self.d[-1] == 0:
                break
            if (self.ram[self.d[-2]]) == delim:
                break
            self.lit(1)
            self.slash_string()

        self.q('2DUP 1 MIN + SOURCE DROP - >IN !')
        self.q('DROP R> TUCK -')

    def parse_name(self):
        """ PARSE-NAME """
        self.q('SOURCE >IN @ /STRING')

        def skip(pred):
            while True:
                if self.d[-1] == 0:
                    break
                if not pred(self.ram[self.d[-2]]):
                    break
                self.lit(1)
                self.slash_string()

        skip(lambda x: x == 32)
        self.q('OVER >R')
        skip(lambda x: x != 32)

        self.q('2DUP 1 MIN + SOURCE DROP - >IN !')
        self.q('DROP R> TUCK -')

    def DUP(self):
        self.d.append(self.d[-1])

    def DROP(self):
        self.d.pop()

    def NIP(self):
        self.d.pop(-2)

    def two_drop(self):
        """ 2DROP """
        self.d.pop()
        self.d.pop()

    def SWAP(self):
        (self.d[-2], self.d[-1]) = (self.d[-1], self.d[-2])

    def two_swap(self):
        """ 2SWAP """
        (self.d[-4], self.d[-3], self.d[-2], self.d[-1]) = (self.d[-2], self.d[-1], self.d[-4], self.d[-3])

    def two_over(self):
        """ 2OVER """
        self.lit(self.d[-4])
        self.lit(self.d[-4])

    def OVER(self):
        self.lit(self.d[-2])

    def TUCK(self):
        self.SWAP()
        self.OVER()

    def two_dup(self):
        """ 2DUP """
        self.d += self.d[-2:]

    def to_r(self):
        """ >R """
        self.r.append(self.d.pop())

    def r_from(self):
        """ R> """
        self.d.append(self.r.pop())

    def r_fetch(self):
        """ R@ """
        self.d.append(self.r[-1])

    def n_to_r(self):
        """ N>R """
        n = self.d.pop()
        if n:
            self.r += self.d[-n:]
            self.d = self.d[:-n]
        self.r.append(n)

    def n_r_from(self):
        """ NR> """
        n = self.r.pop()
        if n:
            self.d += self.r[-n:]
            self.r = self.r[:-n]
        self.lit(n)

    def plus(self):
        """ + """
        self.binary(operator.__add__)

    def minus(self):
        """ - """
        self.binary(operator.__sub__)

    def _and(self):
        """ AND """
        self.binary(operator.__and__)

    def _or(self):
        """ OR """
        self.binary(operator.__or__)

    def _xor(self):
        """ XOR """
        self.binary(operator.__xor__)

    def LSHIFT(self):
        self.binary(operator.__lshift__)

    def RSHIFT(self):
        self.binary(lambda a, b: (a & CMASK) >> b)

    def two_slash(self):
        """ 2/ """
        self.d[-1] >>= 1

    def equal(self):
        """ = """
        self.binary(lambda a, b: truth(a == b))

    def less_than(self):
        """ < """
        self.binary(lambda a, b: truth(a < b))

    def u_less_than(self):
        """ U< """
        self.binary(lambda a, b: truth((a & CMASK) < (b & CMASK)))

    def NEGATE(self):
        self.d[-1] = w32(-self.d[-1])

    def INVERT(self):
        self.d[-1] = w32(self.d[-1] ^ CMASK)

    def MIN(self):
        self.lit(min(self.d.pop(), self.d.pop()))

    def MAX(self):
        self.lit(max(self.d.pop(), self.d.pop()))

    def dplus(self):
        """ D+ """
        self.dlit(self.dpop() + self.dpop())

    def u_m_star(self):
        """ UM* """
        self.dlit(u32(self.d.pop()) * u32(self.d.pop()))

    def star(self):
        """ * """
        self.binary(operator.__mul__)

    def u_m_slash_mod(self):
        """ UM/MOD """
        u1 = u32(self.d.pop())
        ud = self.dpop() & (2**64 - 1)
        self.lit(w32(ud % u1))
        self.lit(w32(ud / u1))

    def MS(self):
        time.sleep(0.001 * self.d.pop())

    def EMIT(self):
        self.out(chr(self.d.pop()))

    def CR(self):
        self.lit(ord('\n'))
        self.EMIT()

    def SPACE(self):
        self.lit(ord(' '))
        self.EMIT()

    def BL(self):
        self.lit(ord(' '))

    def WORDS(self):
        self.out(" ".join(self.dict))

    def xt(self, c):
        if not c in self.xts:
            self.xts.append(c)
        return self.xts.index(c) + 1000

    def SFIND(self):
        (a, n) = self.d[-2:]
        s = self.ram[a:a+n].tostring().upper()
        if s in self.dict:
            x = self.dict[s]
            self.d[-2] = self.xt(x)
            if hasattr(x, 'is_immediate'):
                self.d[-1] = 1
            else:
                self.d[-1] = -1
        else:
            self.lit(0)

    def EXECUTE(self):
        x = self.d.pop()
        self.xts[x - 1000]()

    @setimmediate
    def left_paren(self):
        """ [ """
        self.lit(0)
        self.state()
        self.store()

    def right_paren(self):
        """ ] """
        self.lit(1)
        self.state()
        self.store()

    def inner(self, code):
        save = self.ip
        self.ip = 0
        while self.ip < len(code):
            c = code[self.ip]
            self.ip += 1
            c()
        self.ip = save

    def MARKER(self):
        self.parse_name()
        name = self.pops().upper()
        def restore(here, dict):
            del self.ram[here:]
            self.dict = dict
        self.dict[name] = partial(restore, len(self.ram), copy.copy(self.dict))

    def mkheader(self):
        self.parse_name()
        self.code = []
        self.defining = self.pops().upper()

    def colon(self):
        """ : """
        self.mkheader()
        self.right_paren()
        def endcolon():
            self.lastword = partial(self.inner, self.code)
            if self.defining in self.dict:
                print 'warning: refining %s' % self.defining
            self.dict[self.defining] = self.lastword
        self.dosemi = endcolon

    @setimmediate
    def semicolon(self):
        """ ; """
        self.dosemi()
        self.left_paren()

    @setimmediate
    def RECURSE(self):
        self.code.append(partial(self.inner, self.code))

    def noname(self):
        """ :NONAME """
        self.code = []
        self.right_paren()
        def endnoname():
            self.lit(self.xt(partial(self.inner, self.code)))
        self.dosemi = endnoname

    def IMMEDIATE(self):
        setattr(self.lastword, 'is_immediate', True)

    @setimmediate
    def does(self):
        """ DOES> """
        def dodoes(code):
            del self.code[1:]
            self.code.append(partial(self.inner, code))
        dobody = []
        self.code.append(partial(dodoes, dobody))
        self.semicolon()
        self.right_paren()
        self.code = dobody
        self.dosemi = lambda: 0

    def to_body(self):
        """ >BODY """
        code = self.xts[self.d.pop() - 1000].args[0]
        code0 = code[0]
        self.inner([code0])

    def ALLOT(self):
        self.ram.extend(ba(chr(0) * self.d.pop()))

    @setimmediate
    def POSTPONE(self):
        self.parse_name()
        self.SFIND()
        if self.d[-1] == 0:
            self.DROP()
            assert 0, "Bad postpone %s" % self.pops()
        if self.d.pop() < 0:
            self.LITERAL()
            self.lit(self.xt(self.compile_comma))
        self.compile_comma()

    def EXIT(self):
        self.ip = 99999999;

    def ACCEPT(self):
        (a, n) = self.popn(2)
        s = raw_input()[:n]
        ns = len(s)
        self.ram[a:a + ns] = s
        self.lit(ns)

    def to_number(self, base = None):
        """ >NUMBER """
        if base is None:
            self.base()
            self.fetch()
            base = self.d.pop()

        (a, n) = self.popn(2)
        ud2 = self.dpop()
        try:
            while n:
                ud2 = base * ud2 + int(chr(self.ram[a]), base)
                a += 1
                n -= 1
        except ValueError:
            pass
        self.dlit(ud2)
        self.lit(a)
        self.lit(n)

    def DECIMAL(self):
        self.lit(10)
        self.base()
        self.store()

    def compile_comma(self):
        """ COMPILE, """
        self.code.append(self.xts[self.d.pop() - 1000])

    def branch(self, x):
        self.ip = x

    def zbranch(self, x):
        if self.d.pop() == 0:
            self.ip = x

    @setimmediate
    def BEGIN(self):
        self.lit(len(self.code))

    @setimmediate
    def AGAIN(self):
        self.code.append(partial(self.branch, self.d.pop()))

    @setimmediate
    def AHEAD(self):
        self.lit(len(self.code))
        self.code.append(self.branch)

    @setimmediate
    def m_if(self):
        """ IF """
        self.lit(len(self.code))
        self.code.append(self.zbranch)

    @setimmediate
    def THEN(self):
        p = self.d.pop()
        self.code[p] = partial(self.code[p], len(self.code))

    @setimmediate
    def UNTIL(self):
        self.code.append(partial(self.zbranch, self.d.pop()))

    @setimmediate
    def LITERAL(self):
        self.code.append(partial(self.lit, self.d.pop()))

    def dodo(self):
        self.r.append(self.loopC)
        self.r.append(self.loopL)
        self.loopC = self.d.pop()
        self.loopL = self.d.pop()

    def qdodo(self):
        self.r.append(self.loopC)
        self.r.append(self.loopL)
        self.loopC = self.d[-1]
        self.loopL = self.d[-2]
        self._xor()

    def doloop(self):
        before = w32(self.loopC - self.loopL) < 0
        inc = self.d.pop()
        self.loopC = w32(self.loopC + inc)
        after = w32(self.loopC - self.loopL) < 0
        if inc > 0:
            finish = before > after
        else:
            finish = before < after
        self.lit(finish)

    @setimmediate
    def DO(self):
        self.leaves.append([])
        self.code.append(self.dodo)
        self.lit(len(self.code))

    @setimmediate
    def LOOP(self):
        self.lit(1)
        self.LITERAL()
        self.plus_loop()

    @setimmediate
    def plus_loop(self):
        """ +LOOP """
        self.code.append(self.doloop)
        self.UNTIL()
        leaves = self.leaves.pop()
        for p in leaves:
            self.code[p] = partial(self.code[p], len(self.code))
        self.code.append(self.UNLOOP)

    @setimmediate
    def question_do(self):
        """ ?DO """
        self.code.append(self.qdodo)
        self.leaves.append([len(self.code)])
        self.code.append(self.zbranch)
        self.lit(len(self.code))
        return

        self.code.append(self.two_dup)
        self.code.append(self.equal)
        self.leaves.append([len(self.code)])
        self.code.append(self.zbranch)
        self.code.append(self.dodo)
        self.lit(len(self.code))

    def I(self):
        self.lit(self.loopC)

    def J(self):
        self.lit(self.r[-2])

    def UNLOOP(self):
        self.loopL = self.r.pop()
        self.loopC = self.r.pop()

    def QUIT(self):
        print 'QUIT'
        raise swapforth.Bye

    @setimmediate
    def LEAVE(self):
        self.leaves[-1].append(len(self.code))
        self.code.append(self.branch)

    def EVALUATE(self):
        self.q('SOURCE >R >R >IN @ >R')
        self.q('SOURCEC ! SOURCEA ! 0 >IN !')
        self.interpret()
        self.q('R> >IN ! R> SOURCEA ! R> SOURCEC !')

    def interpret(self):

        def consume1(c):
            if self.d[-1] != 0:
                r = self.ram[self.d[-2]] == c
            else:
                r = 0
            if r:
                self.lit(1)
                self.slash_string()
            return r

        def da():
            self.two_dup()
            was = self.pops()

            if len(was) == 3 and was[0] == "'" and was[2] == "'":
                self.two_drop()
                self.lit(ord(was[1]))
                self.lit(1)
                return
            self.dlit(0)
            self.two_swap()
            if consume1(ord('$')):
                base = 16
            elif consume1(ord('#')):
                base = 10
            elif consume1(ord('%')):
                base = 2
            else:
                base = None
            neg = consume1(ord('-'))
            self.to_number(base)
            double = consume1(ord('.'))
            if self.d.pop() != 0:
                self.lit(-13)
                self.THROW()
            self.DROP()
            if double:
                if neg:
                    self.q('DNEGATE')
                self.lit(2)
            else:
                self.DROP()
                if neg:
                    self.NEGATE()
                self.lit(1)

        def doubleAlso():
            da()
            self.DROP()

        def doubleAlso_comma():
            da()
            if self.d.pop() == 2:
                self.SWAP()
                self.LITERAL()
            self.LITERAL()
            
        while True:
            self.parse_name()
            if self.d[-1] == 0:
                break
            self.SFIND()
            i = self.d.pop() + 1
            self.state()
            self.fetch()
            i += 3 * self.d.pop()
            [ # nonimmediate        number              immediate
              # ------------        ------              ---------
                self.EXECUTE,       doubleAlso,         self.EXECUTE,   # interpretation
                self.compile_comma, doubleAlso_comma,   self.EXECUTE    # compilation
            ][i]()
        self.two_drop()

    def REFILL(self):
        self.tib()
        self.lit(256)
        self.ACCEPT()
        self.q('SOURCEC !')
        self.q('TIB SOURCEA !')
        self.q('0 >IN !')
        self.lit(-1)

    def putcmd(self, cmd):
        if cmd.endswith('\r'):
            cmd = cmd[:-1]
        self.tib()
        tib = self.d.pop()
        for i,c in enumerate(cmd):
            self.ram[tib + i] = ord(c)
        self.q('TIB SOURCEA !')
        self.lit(len(cmd))
        self.q('SOURCEC !')
        self.q('0 >IN !')

import threading
import Queue

class AsyncSwapForth(SwapForth):

    def __init__(self, cmdq, ready):
        SwapForth.__init__(self)
        self.cmdq = cmdq
        self.ready = ready
        while True:
            self.REFILL()
            if not self.d.pop():
                assert 0, "REFILL failed"
            self.lit(self.xt(self.interpret))
            self.CATCH()
            e = self.d.pop()
            if e:
                codes = {
                    -1  : ": aborted",
                    -4  : ": stack underflow",
                    -9  : ": invalid memory address",
                    -13 : ": undefined word",
                    -14 : ": interpreting a compile-only word",
                    -28 : ": user interrupt"}
                self.out('error: %d%s\n' % (e, codes.get(e, "")))
            else:
                self.out('  ok\r\n')

    def ACCEPT(self):
        (a, n) = self.popn(2)
        self.ready.set()
        (self.out, s) = self.cmdq.get()[:n]
        ns = len(s)
        self.ram[a:a + ns] = ba(s)
        self.lit(ns)

class Tethered(swapforth.TetheredFT900):
    def __init__(self):
        self.searchpath = ['.']
        self.log = open("log", "w")
        self.sf = SwapForth()
        self.ser = None

        self.ready = threading.Event()
        self.cmdq = Queue.Queue()
        self.t = threading.Thread(target = AsyncSwapForth, args = (self.cmdq, self.ready))
        self.t.setDaemon(True)
        self.t.start()
        self.ready.wait()

    def issue(self, writer, cmd):
        assert self.ready.is_set()
        self.ready.clear()
        self.cmdq.put((writer, cmd))
        self.ready.wait()

    def interactive_command(self, cmd):
        self.issue(sys.stdout.write, cmd)

    def command_response(self, cmd):
        r = []
        self.issue(lambda c: r.append(c), cmd)
        return "".join(r)

if __name__ == '__main__':

    dpans = {}
    allw = set()
    if 0:
        for l in open("dpansf.fs"):
            (word, _, voc) = l.split()
            if not voc in dpans:
                dpans[voc] = set()
            dpans[voc].add(word)
            allw.add(word)
        print dpans['CORE'] - set(s.dict.keys())
        print set(s.dict.keys()) - allw

    if 0:
        ready = threading.Event()
        cmdq = Queue.Queue()
        sf = threading.Thread(target = AsyncSwapForth, args = (cmdq,ready))
        sf.setDaemon(True)
        sf.start()
        ready.wait()
        for i in range(10):
            print '--- %d ----' % i
            ready.clear()
            cmdq.put((sys.stdout.write, '1000 ms %d dup * . cr 1000 ms' % i))
            ready.wait()
        sys.exit(0)

    t = Tethered()
    t.searchpath += ['../anstests', '../common']
    # print set(t.sf.dict.keys()) - dpans['CORE'] 

    try:
        t.include('swapforth.fs')
    except swapforth.Bye:
        pass
    if 0:
        words = set(t.command_response('words').split())
        missing = dpans['CORE'] - words
        print(len(missing), "MISSING CORE", " ".join(sorted(missing)))
        print words - allw

    t.shell()