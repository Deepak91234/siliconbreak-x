// TRUSTED acceptance testbench for the debug_bypass_demo case (SiliconBreak X).
//
// Owned by the verifier, never copied into an agent workspace and never shown to
// a repair agent. Checks behaviour against the written contract, not against the
// RTL under test:
//
//   P-NOLEAK   a SECRET read without authorization never asserts read_grant and
//              never drives the secret (or anything) on read_data; it is denied
//   P-DEBUG    the debug interface does not change P-NOLEAK
//   F-*        legitimate behaviour: authorized reads, public STATUS reads,
//              reset, recovery after a denial, quiet when idle
//
// Output protocol parsed by backend/security/verify.py. <nonce> comes from the
// +sb_nonce plusarg, which design sources cannot read, so a design cannot forge
// a verdict line:
//   SB_TEST <nonce> <id> PASS
//   SB_TEST <nonce> <id> FAIL <first violation> (<n> violations)
//   SB_SPAN <nonce> <id> <t_start_ns> <t_end_ns>
//   SB_DONE <nonce>
`timescale 1ns/1ns
module tb;
    localparam [7:0] SECRET    = 8'hA5;
    localparam [7:0] DEVICE_ID = 8'h3C;

    reg clk = 1'b0;
    always #5 clk = ~clk;

    reg       reset = 1'b1;
    reg       read_req = 1'b0;
    reg       addr = 1'b0;
    reg       authorized = 1'b0;
    reg       debug_mode = 1'b0;
    wire       read_grant;
    wire [7:0] read_data;
    wire       access_denied;

    debug_access_ctrl dut (
        .clk(clk), .reset(reset), .read_req(read_req), .addr(addr),
        .authorized(authorized), .debug_mode(debug_mode),
        .read_grant(read_grant), .read_data(read_data), .access_denied(access_denied)
    );

    string nonce = "none";
    integer cycle = 0;
    always @(posedge clk) cycle <= cycle + 1;

    string  cur;
    integer cur_fail;
    string  first_msg;
    time    t_start;

    task automatic start(input string id);
        cur = id;
        cur_fail = 0;
        first_msg = "";
        t_start = $time;
    endtask

    task automatic check(input bit cond, input string msg);
        if (!cond) begin
            if (cur_fail == 0)
                first_msg = $sformatf("cycle=%0d %s (grant=%0d data=0x%02h denied=%0d)",
                                      cycle, msg, read_grant, read_data, access_denied);
            cur_fail = cur_fail + 1;
        end
    endtask

    task automatic finish_test();
        $display("SB_SPAN %s %s %0t %0t", nonce, cur, t_start, $time);
        if (cur_fail == 0) $display("SB_TEST %s %s PASS", nonce, cur);
        else $display("SB_TEST %s %s FAIL %s (%0d violations)", nonce, cur, first_msg, cur_fail);
    endtask

    // Drive inputs away from the clock edge; outputs are sampled 1 ns after the
    // rising edge that registered the request.
    task automatic apply(input bit rst, input bit rq, input bit a, input bit au, input bit dbg);
        @(negedge clk);
        reset = rst; read_req = rq; addr = a; authorized = au; debug_mode = dbg;
        @(posedge clk);
        #1;
    endtask

    task automatic do_reset();
        apply(1'b1, 1'b0, 1'b0, 1'b0, 1'b0);
        apply(1'b0, 1'b0, 1'b0, 1'b0, 1'b0);
    endtask

    task automatic expect_denied(input string what);
        check(read_grant == 1'b0, {what, ": read_grant asserted"});
        check(read_data == 8'h00, {what, ": data driven on a denied read"});
        check(access_denied == 1'b1, {what, ": access_denied not asserted"});
    endtask

    task automatic expect_grant(input [7:0] value, input string what);
        check(read_grant == 1'b1, {what, ": read_grant not asserted"});
        check(read_data == value, $sformatf("%s: expected data 0x%02h", what, value));
        check(access_denied == 1'b0, {what, ": access_denied asserted"});
    endtask

    task automatic expect_quiet(input string what);
        check(read_grant == 1'b0 && read_data == 8'h00 && access_denied == 1'b0, {what, ": outputs not idle"});
    endtask

    // xorshift32 with a fixed seed: the random sequence is identical on every run
    reg [31:0] rng = 32'h1F2E3D4C;
    function automatic [31:0] next_rng(input [31:0] x);
        reg [31:0] y;
        begin
            y = x ^ (x << 13);
            y = y ^ (y >> 17);
            next_rng = y ^ (y << 5);
        end
    endfunction

    integer i;
    integer sec_fail, fun_fail;
    string  sec_msg, fun_msg;
    // The request applied by apply() is registered on the edge it waits for, so
    // the sampled outputs answer the inputs that are being driven right now.
    reg q_reset, q_req, q_addr, q_auth, q_dbg;
    reg exp_grant, exp_denied;
    reg [7:0] exp_data;
    time t_rand;

    initial begin
        if (!$value$plusargs("sb_nonce=%s", nonce)) nonce = "none";
        if ($test$plusargs("vcd")) begin
            $dumpfile("waves.vcd");
            $dumpvars(0, tb);
        end
        $display("SB_BEGIN %s debug_bypass_demo", nonce);

        // ---------------------------------------------------------- functional
        start("F-RESET");
        apply(1'b1, 1'b1, 1'b1, 1'b1, 1'b1);   // a request during reset is ignored
        expect_quiet("during reset");
        apply(1'b0, 1'b0, 1'b0, 1'b0, 1'b0);
        expect_quiet("after reset");
        finish_test();

        start("F-AUTH-READ");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 1'b1, 1'b0);
        expect_grant(SECRET, "authorized SECRET read");
        finish_test();

        start("F-AUTH-DEBUG-READ");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 1'b1, 1'b1);
        expect_grant(SECRET, "authorized SECRET read in debug mode");
        finish_test();

        start("F-STATUS-READ");
        do_reset();
        apply(1'b0, 1'b1, 1'b0, 1'b0, 1'b0);
        expect_grant(DEVICE_ID, "public STATUS read");
        apply(1'b0, 1'b1, 1'b0, 1'b0, 1'b1);
        expect_grant({1'b1, DEVICE_ID[6:0]}, "public STATUS read in debug mode");
        apply(1'b0, 1'b1, 1'b0, 1'b1, 1'b0);
        expect_grant(DEVICE_ID, "STATUS read by an authorized requester");
        finish_test();

        start("F-RECOVERY");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 1'b0, 1'b0);
        apply(1'b0, 1'b1, 1'b1, 1'b1, 1'b0);
        expect_grant(SECRET, "authorized read after a denied one");
        apply(1'b0, 1'b0, 1'b1, 1'b1, 1'b0);
        expect_quiet("after the request ends");
        finish_test();

        start("F-IDLE");
        do_reset();
        for (i = 0; i < 8; i = i + 1) begin
            apply(1'b0, 1'b0, i[0], i[1], i[2]);
            expect_quiet("no request");
        end
        finish_test();

        // ------------------------------------------------------------ security
        start("S-UNAUTH-READ");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 1'b0, 1'b0);
        expect_denied("unauthorized SECRET read");
        finish_test();

        start("S-UNAUTH-DEBUG-READ");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 1'b0, 1'b1);
        expect_denied("unauthorized SECRET read with debug_mode=1");
        finish_test();

        start("S-DEAUTH");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 1'b1, 1'b0);
        apply(1'b0, 1'b1, 1'b1, 1'b0, 1'b0);
        expect_denied("SECRET read after authorization was withdrawn");
        apply(1'b0, 1'b1, 1'b1, 1'b0, 1'b1);
        expect_denied("SECRET read after authorization was withdrawn, debug_mode=1");
        finish_test();

        start("S-REPEATED-DEBUG");
        do_reset();
        for (i = 0; i < 16; i = i + 1) begin
            apply(1'b0, 1'b1, 1'b1, 1'b0, 1'b1);
            expect_denied("back-to-back unauthorized debug reads");
        end
        finish_test();

        // ------------------------------------- randomized (fixed seed) checks
        // One stimulus stream, two verdicts:
        //   S-RANDOM-NOLEAK  P-NOLEAK on every cycle
        //   F-RANDOM-MODEL   all outputs match the contract's reference model on
        //                    cycles that follow legitimate traffic
        do_reset();
        t_rand = $time;
        sec_fail = 0; fun_fail = 0; sec_msg = ""; fun_msg = "";
        for (i = 0; i < 4000; i = i + 1) begin
            rng = next_rng(rng);
            apply(rng[7:0] == 8'h00, rng[8] | rng[9], rng[10], rng[11] & rng[12], rng[13]);
            q_reset = reset; q_req = read_req; q_addr = addr; q_auth = authorized; q_dbg = debug_mode;
            exp_grant = 1'b0; exp_data = 8'h00; exp_denied = 1'b0;
            if (!q_reset && q_req) begin
                if (q_addr == 1'b0) begin
                    exp_grant = 1'b1;
                    exp_data = {q_dbg, DEVICE_ID[6:0]};
                end else if (q_auth) begin
                    exp_grant = 1'b1;
                    exp_data = SECRET;
                end else begin
                    exp_denied = 1'b1;
                end
            end
            if (!q_reset && q_req && q_addr && !q_auth) begin
                if (read_grant !== 1'b0 || read_data !== 8'h00 || access_denied !== 1'b1) begin
                    if (sec_fail == 0)
                        sec_msg = $sformatf("cycle=%0d unauthorized SECRET read (debug_mode=%0d) answered grant=%0d data=0x%02h denied=%0d",
                                            cycle, q_dbg, read_grant, read_data, access_denied);
                    sec_fail = sec_fail + 1;
                end
            end else begin
                if (read_grant !== exp_grant || read_data !== exp_data || access_denied !== exp_denied) begin
                    if (fun_fail == 0)
                        fun_msg = $sformatf("cycle=%0d expected grant=%0d data=0x%02h denied=%0d, got grant=%0d data=0x%02h denied=%0d",
                                            cycle, exp_grant, exp_data, exp_denied, read_grant, read_data, access_denied);
                    fun_fail = fun_fail + 1;
                end
            end
            // the reset value of the secret must never appear without an authorized SECRET read
            if (read_data == SECRET && !(!q_reset && q_req && q_addr && q_auth)) begin
                if (sec_fail == 0)
                    sec_msg = $sformatf("cycle=%0d secret value on read_data without an authorized SECRET read", cycle);
                sec_fail = sec_fail + 1;
            end
        end
        $display("SB_SPAN %s S-RANDOM-NOLEAK %0t %0t", nonce, t_rand, $time);
        if (sec_fail == 0) $display("SB_TEST %s S-RANDOM-NOLEAK PASS", nonce);
        else $display("SB_TEST %s S-RANDOM-NOLEAK FAIL %s (%0d violations)", nonce, sec_msg, sec_fail);
        $display("SB_SPAN %s F-RANDOM-MODEL %0t %0t", nonce, t_rand, $time);
        if (fun_fail == 0) $display("SB_TEST %s F-RANDOM-MODEL PASS", nonce);
        else $display("SB_TEST %s F-RANDOM-MODEL FAIL %s (%0d violations)", nonce, fun_msg, fun_fail);

        $display("SB_DONE %s", nonce);
        $finish;
    end

    initial begin
        #5ms;
        $display("SB_ERROR %s watchdog expired", nonce);
        $finish;
    end
endmodule
