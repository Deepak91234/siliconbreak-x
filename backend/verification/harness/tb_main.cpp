// SiliconBreak X generic Basys 3 simulation harness (Verilator).
//
// Works for every SiliconBreak X design because the trusted board wrapper
// (basys3_top) always has the same port list.
//
// usage: Vsim <stimulus.txt> <trace.txt> [vcd_path vcd_start_cycle vcd_end_cycle] [+verilator+...]
//
// stimulus.txt (one directive per line, '#' starts a comment):
//     end <cycles>                 total number of rising clock edges to simulate
//     <cycle> <port> <value>       drive <port> to <value> just before rising edge <cycle>
//   ports: sw (16-bit), btnC, btnU, btnL, btnR, btnD.  value: decimal or 0x-hex.
//
// trace.txt (written by this harness):
//     # siliconbreak-trace v1
//     <cycle> <signal> 0x<hex>     output value after rising edge <cycle> (cycle 0 = initial)
//   signals: led, seg, dp, an. A line is written only when the value changes.
//     end <cycles>
//
// Time base in the VCD: one time unit = half a clock period
// (rising edge k at t = 2k, falling edge before it at t = 2k-1).

#include <verilated.h>
#if VM_TRACE
#include <verilated_vcd_c.h>
#endif
#include "Vbasys3_top.h"

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace {

struct Event {
    uint64_t cycle;
    int port;  // index into kPorts
    uint32_t value;
    size_t order;
};

const char* const kPorts[] = {"sw", "btnC", "btnU", "btnL", "btnR", "btnD"};
constexpr int kNumPorts = 6;

int port_index(const std::string& name) {
    for (int i = 0; i < kNumPorts; ++i)
        if (name == kPorts[i]) return i;
    return -1;
}

bool parse_u64(const std::string& s, uint64_t& out) {
    if (s.empty()) return false;
    errno = 0;
    char* end = nullptr;
    unsigned long long v = std::strtoull(s.c_str(), &end, 0);
    if (errno != 0 || end == s.c_str() || *end != '\0') return false;
    out = v;
    return true;
}

void apply(Vbasys3_top* top, int port, uint32_t value) {
    switch (port) {
        case 0: top->sw = value & 0xFFFFu; break;
        case 1: top->btnC = value & 1u; break;
        case 2: top->btnU = value & 1u; break;
        case 3: top->btnL = value & 1u; break;
        case 4: top->btnR = value & 1u; break;
        case 5: top->btnD = value & 1u; break;
        default: break;
    }
}

int fail(const std::string& msg) {
    std::fprintf(stderr, "CL_HARNESS_ERROR %s\n", msg.c_str());
    return 2;
}

}  // namespace

int main(int argc, char** argv) {
    const std::unique_ptr<VerilatedContext> ctx{new VerilatedContext};
    ctx->commandArgs(argc, argv);  // consumes +verilator+ runtime options

    std::vector<std::string> pos;
    for (int i = 1; i < argc; ++i)
        if (argv[i][0] != '+') pos.emplace_back(argv[i]);
    if (pos.size() != 2 && pos.size() != 5)
        return fail("usage: Vsim <stimulus> <trace> [vcd start end]");

    std::ifstream in(pos[0]);
    if (!in) return fail("cannot open stimulus file " + pos[0]);

    uint64_t end_cycle = 0;
    bool have_end = false;
    std::vector<Event> events;
    std::string line;
    size_t lineno = 0;
    while (std::getline(in, line)) {
        ++lineno;
        const size_t hash = line.find('#');
        if (hash != std::string::npos) line.erase(hash);
        std::istringstream ss(line);
        std::vector<std::string> tok;
        for (std::string t; ss >> t;) tok.push_back(t);
        if (tok.empty()) continue;
        if (tok[0] == "end" && tok.size() == 2) {
            if (!parse_u64(tok[1], end_cycle)) return fail("bad end cycle on line " + std::to_string(lineno));
            have_end = true;
            continue;
        }
        if (tok.size() != 3) return fail("bad stimulus line " + std::to_string(lineno));
        uint64_t cyc = 0, val = 0;
        const int port = port_index(tok[1]);
        if (!parse_u64(tok[0], cyc) || !parse_u64(tok[2], val) || port < 0)
            return fail("bad stimulus line " + std::to_string(lineno));
        events.push_back(Event{cyc, port, static_cast<uint32_t>(val), events.size()});
    }
    if (!have_end) return fail("stimulus has no 'end <cycles>' directive");
    std::stable_sort(events.begin(), events.end(),
                     [](const Event& a, const Event& b) { return a.cycle < b.cycle; });

    std::FILE* out = std::fopen(pos[1].c_str(), "w");
    if (!out) return fail("cannot open trace file " + pos[1]);

    auto top = std::make_unique<Vbasys3_top>(ctx.get());

    bool tracing = false;
    uint64_t vcd_start = 0, vcd_end = 0;
#if VM_TRACE
    std::unique_ptr<VerilatedVcdC> tfp;
    if (pos.size() == 5) {
        if (!parse_u64(pos[3], vcd_start) || !parse_u64(pos[4], vcd_end))
            return fail("bad VCD window");
        ctx->traceEverOn(true);
        tfp = std::make_unique<VerilatedVcdC>();
        top->trace(tfp.get(), 99);
        tfp->open(pos[2].c_str());
        tracing = true;
    }
#else
    if (pos.size() == 5) return fail("harness was built without --trace");
#endif
    auto dump = [&](uint64_t cycle, uint64_t t) {
#if VM_TRACE
        if (tracing && cycle >= vcd_start && cycle <= vcd_end) tfp->dump(t);
#else
        (void)cycle;
        (void)t;
#endif
    };

    top->clk = 0;
    top->sw = 0;
    top->btnC = top->btnU = top->btnL = top->btnR = top->btnD = 0;
    size_t ev = 0;
    while (ev < events.size() && events[ev].cycle == 0) {
        apply(top.get(), events[ev].port, events[ev].value);
        ++ev;
    }
    top->eval();
    dump(0, 0);

    uint32_t led = top->led, seg = top->seg, dp = top->dp, an = top->an;
    std::fprintf(out, "# siliconbreak-trace v1\n");
    std::fprintf(out, "0 led 0x%04x\n0 seg 0x%02x\n0 dp 0x%x\n0 an 0x%x\n", led, seg, dp, an);

    for (uint64_t cycle = 1; cycle <= end_cycle && !ctx->gotFinish(); ++cycle) {
        while (ev < events.size() && events[ev].cycle <= cycle) {
            apply(top.get(), events[ev].port, events[ev].value);
            ++ev;
        }
        top->clk = 0;
        top->eval();
        dump(cycle, 2 * cycle - 1);
        top->clk = 1;
        top->eval();
        dump(cycle, 2 * cycle);
        if (top->led != led) { led = top->led; std::fprintf(out, "%llu led 0x%04x\n", (unsigned long long)cycle, led); }
        if (top->seg != seg) { seg = top->seg; std::fprintf(out, "%llu seg 0x%02x\n", (unsigned long long)cycle, seg); }
        if (top->dp != dp)   { dp = top->dp;   std::fprintf(out, "%llu dp 0x%x\n", (unsigned long long)cycle, dp); }
        if (top->an != an)   { an = top->an;   std::fprintf(out, "%llu an 0x%x\n", (unsigned long long)cycle, an); }
    }
    std::fprintf(out, "end %llu\n", (unsigned long long)end_cycle);
    std::fclose(out);

    top->final();
#if VM_TRACE
    if (tracing) tfp->close();
#endif
    return 0;
}
