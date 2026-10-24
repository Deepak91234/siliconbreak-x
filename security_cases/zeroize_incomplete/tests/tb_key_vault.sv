// TRUSTED acceptance testbench for the zeroize_incomplete case (SiliconBreak X).
// Never copied into an agent workspace. Verdict lines carry the +sb_nonce value.
//
//   P-ZEROIZE     after zeroize, key_out == 0 and key_valid == 0
//   P-NO-RESIDUE  whenever key_valid == 0, key_out == 0 (no key material remains)
//   F-*           reset, load, reload after zeroize, hold
`timescale 1ns/1ns
module tb;
    reg clk = 1'b0;
    always #5 clk = ~clk;

    reg         reset = 1'b1;
    reg         load = 1'b0;
    reg  [15:0] key_in = 16'h0000;
    reg         zeroize = 1'b0;
    wire [15:0] key_out;
    wire        key_valid;

    key_vault dut (.clk(clk), .reset(reset), .load(load), .key_in(key_in), .zeroize(zeroize),
                   .key_out(key_out), .key_valid(key_valid));

    string nonce = "none";
    integer cycle = 0;
    always @(posedge clk) cycle <= cycle + 1;

    string  cur;
    integer cur_fail;
    string  first_msg;
    time    t_start;

    task automatic start(input string id);
        cur = id; cur_fail = 0; first_msg = ""; t_start = $time;
    endtask

    task automatic check(input bit cond, input string msg);
        if (!cond) begin
            if (cur_fail == 0) first_msg = $sformatf("cycle=%0d %s (key_out=0x%04h key_valid=%0d)", cycle, msg, key_out, key_valid);
            cur_fail = cur_fail + 1;
        end
    endtask

    task automatic finish_test();
        $display("SB_SPAN %s %s %0t %0t", nonce, cur, t_start, $time);
        if (cur_fail == 0) $display("SB_TEST %s %s PASS", nonce, cur);
        else $display("SB_TEST %s %s FAIL %s (%0d violations)", nonce, cur, first_msg, cur_fail);
    endtask

    task automatic apply(input bit rst, input bit ld, input [15:0] k, input bit z);
        @(negedge clk);
        reset = rst; load = ld; key_in = k; zeroize = z;
        @(posedge clk);
        #1;
    endtask

    task automatic idle();
        apply(1'b0, 1'b0, 16'h0000, 1'b0);
    endtask

    task automatic do_reset();
        apply(1'b1, 1'b0, 16'h0000, 1'b0);
        idle();
    endtask

    reg [31:0] rng = 32'h5EED1234;
    function automatic [31:0] next_rng(input [31:0] x);
        reg [31:0] y;
        begin
            y = x ^ (x << 13);
            y = y ^ (y >> 17);
            next_rng = y ^ (y << 5);
        end
    endfunction

    integer i, sec_fail, fun_fail;
    string sec_msg, fun_msg;
    reg [15:0] m_key;
    reg m_valid;
    time t_rand;

    initial begin
        if (!$value$plusargs("sb_nonce=%s", nonce)) nonce = "none";
        if ($test$plusargs("vcd")) begin
            $dumpfile("waves.vcd");
            $dumpvars(0, tb);
        end

        start("F-RESET");
        apply(1'b1, 1'b1, 16'hBEEF, 1'b0);
        check(key_out == 16'h0000 && key_valid == 1'b0, "reset did not clear the key");
        idle();
        check(key_out == 16'h0000 && key_valid == 1'b0, "key appeared after reset");
        finish_test();

        start("F-LOAD");
        do_reset();
        apply(1'b0, 1'b1, 16'hBEEF, 1'b0);
        check(key_out == 16'hBEEF && key_valid == 1'b1, "load did not capture the key");
        idle();
        check(key_out == 16'hBEEF && key_valid == 1'b1, "key not held");
        finish_test();

        start("F-RELOAD");
        do_reset();
        apply(1'b0, 1'b1, 16'hBEEF, 1'b0);
        apply(1'b0, 1'b0, 16'h0000, 1'b1);
        apply(1'b0, 1'b1, 16'h1234, 1'b0);
        check(key_out == 16'h1234 && key_valid == 1'b1, "key could not be reloaded after zeroize");
        finish_test();

        start("S-ZEROIZE");
        do_reset();
        apply(1'b0, 1'b1, 16'hBEEF, 1'b0);
        apply(1'b0, 1'b0, 16'h0000, 1'b1);
        check(key_valid == 1'b0, "key still valid after zeroize");
        check(key_out == 16'h0000, "key material remains after zeroize");
        finish_test();

        start("S-ZEROIZE-WINS");
        do_reset();
        apply(1'b0, 1'b1, 16'hA5C3, 1'b0);
        apply(1'b0, 1'b1, 16'hFFFF, 1'b1);   // load and zeroize together: zeroize wins
        check(key_valid == 1'b0 && key_out == 16'h0000, "simultaneous load overrode zeroize");
        finish_test();

        start("S-ZEROIZE-HOLD");
        do_reset();
        apply(1'b0, 1'b1, 16'hF00D, 1'b0);
        apply(1'b0, 1'b0, 16'h0000, 1'b1);
        for (i = 0; i < 8; i = i + 1) begin
            idle();
            check(key_out == 16'h0000, "key material visible after zeroize");
        end
        finish_test();

        do_reset();
        t_rand = $time;
        sec_fail = 0; fun_fail = 0; sec_msg = ""; fun_msg = "";
        m_key = 16'h0000; m_valid = 1'b0;
        for (i = 0; i < 3000; i = i + 1) begin
            rng = next_rng(rng);
            apply(rng[9:0] == 10'h000, rng[10] & rng[11], rng[31:16], rng[12] & rng[13] & rng[14]);
            if (reset || zeroize) begin
                m_key = 16'h0000; m_valid = 1'b0;
            end else if (load) begin
                m_key = key_in; m_valid = 1'b1;
            end
            if (key_valid === 1'b0 && key_out !== 16'h0000) begin
                if (sec_fail == 0)
                    sec_msg = $sformatf("cycle=%0d key_valid=0 but key_out=0x%04h", cycle, key_out);
                sec_fail = sec_fail + 1;
            end
            if (m_valid && (key_out !== m_key || key_valid !== 1'b1)) begin
                if (fun_fail == 0)
                    fun_msg = $sformatf("cycle=%0d expected valid key 0x%04h, got 0x%04h valid=%0d", cycle, m_key, key_out, key_valid);
                fun_fail = fun_fail + 1;
            end
        end
        $display("SB_SPAN %s S-RANDOM-RESIDUE %0t %0t", nonce, t_rand, $time);
        if (sec_fail == 0) $display("SB_TEST %s S-RANDOM-RESIDUE PASS", nonce);
        else $display("SB_TEST %s S-RANDOM-RESIDUE FAIL %s (%0d violations)", nonce, sec_msg, sec_fail);
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
