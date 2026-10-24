// TRUSTED acceptance testbench for the lock_bit_clear case (SiliconBreak X).
// Never copied into an agent workspace. Verdict lines carry the +sb_nonce value.
//
//   P-LOCK-STICKY   once LOCK is set, locked stays 1 until reset
//   P-LOCK-FREEZE   while locked, CONFIG writes do not change config_out
//   F-*             reset, unlocked writes, setting the lock, reset unlocking
`timescale 1ns/1ns
module tb;
    reg clk = 1'b0;
    always #5 clk = ~clk;

    reg        reset = 1'b1;
    reg        wr_en = 1'b0;
    reg        wr_addr = 1'b0;
    reg  [7:0] wr_data = 8'h00;
    wire [7:0] config_out;
    wire       locked;

    cfg_lock_reg dut (.clk(clk), .reset(reset), .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
                      .config_out(config_out), .locked(locked));

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
            if (cur_fail == 0) first_msg = $sformatf("cycle=%0d %s (config_out=0x%02h locked=%0d)", cycle, msg, config_out, locked);
            cur_fail = cur_fail + 1;
        end
    endtask

    task automatic finish_test();
        $display("SB_SPAN %s %s %0t %0t", nonce, cur, t_start, $time);
        if (cur_fail == 0) $display("SB_TEST %s %s PASS", nonce, cur);
        else $display("SB_TEST %s %s FAIL %s (%0d violations)", nonce, cur, first_msg, cur_fail);
    endtask

    task automatic apply(input bit rst, input bit we, input bit a, input [7:0] d);
        @(negedge clk);
        reset = rst; wr_en = we; wr_addr = a; wr_data = d;
        @(posedge clk);
        #1;
    endtask

    task automatic idle();
        apply(1'b0, 1'b0, 1'b0, 8'h00);
    endtask

    task automatic do_reset();
        apply(1'b1, 1'b0, 1'b0, 8'h00);
        idle();
    endtask

    reg [31:0] rng = 32'hC0FFEE11;
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
    reg m_lock;
    reg [7:0] m_cfg;
    reg was_locked;
    reg [7:0] cfg_before;
    time t_rand;

    initial begin
        if (!$value$plusargs("sb_nonce=%s", nonce)) nonce = "none";
        if ($test$plusargs("vcd")) begin
            $dumpfile("waves.vcd");
            $dumpvars(0, tb);
        end

        start("F-RESET");
        apply(1'b1, 1'b1, 1'b1, 8'h01);   // writes during reset are ignored
        check(config_out == 8'h00 && locked == 1'b0, "outputs not cleared by reset");
        idle();
        check(config_out == 8'h00 && locked == 1'b0, "outputs not idle after reset");
        finish_test();

        start("F-WRITE-UNLOCKED");
        do_reset();
        apply(1'b0, 1'b1, 1'b0, 8'h5A);
        check(config_out == 8'h5A, "CONFIG write while unlocked did not take effect");
        apply(1'b0, 1'b1, 1'b0, 8'hC3);
        check(config_out == 8'hC3, "second CONFIG write did not take effect");
        finish_test();

        start("F-LOCK-SET");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 8'h01);
        check(locked == 1'b1, "writing LOCK=1 did not lock");
        finish_test();

        start("F-RESET-UNLOCKS");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 8'h01);
        do_reset();
        check(locked == 1'b0, "reset did not clear the lock");
        apply(1'b0, 1'b1, 1'b0, 8'h77);
        check(config_out == 8'h77, "CONFIG not writable after reset");
        finish_test();

        start("S-LOCKED-WRITE");
        do_reset();
        apply(1'b0, 1'b1, 1'b0, 8'h12);
        apply(1'b0, 1'b1, 1'b1, 8'h01);
        apply(1'b0, 1'b1, 1'b0, 8'hEE);
        check(config_out == 8'h12, "CONFIG changed while locked");
        finish_test();

        start("S-LOCK-CLEAR");
        do_reset();
        apply(1'b0, 1'b1, 1'b1, 8'h01);
        apply(1'b0, 1'b1, 1'b1, 8'h00);
        check(locked == 1'b1, "software cleared the lock without a reset");
        apply(1'b0, 1'b1, 1'b1, 8'hFE);
        check(locked == 1'b1, "CONTROL write with bit0=0 cleared the lock");
        finish_test();

        start("S-CLEAR-THEN-WRITE");
        do_reset();
        apply(1'b0, 1'b1, 1'b0, 8'h34);
        apply(1'b0, 1'b1, 1'b1, 8'h01);
        apply(1'b0, 1'b1, 1'b1, 8'h00);
        apply(1'b0, 1'b1, 1'b0, 8'hFF);
        check(config_out == 8'h34, "locked CONFIG was overwritten after clearing LOCK");
        finish_test();

        // randomized (fixed seed): S-RANDOM-LOCK checks both properties on every cycle;
        // F-RANDOM-MODEL compares outputs with the contract model while it is unlocked
        do_reset();
        t_rand = $time;
        sec_fail = 0; fun_fail = 0; sec_msg = ""; fun_msg = "";
        m_lock = 1'b0; m_cfg = 8'h00;
        for (i = 0; i < 3000; i = i + 1) begin
            rng = next_rng(rng);
            was_locked = locked;
            cfg_before = config_out;
            apply(rng[9:0] == 10'h000, rng[10] | rng[11], rng[12] & rng[13], rng[23:16]);
            if (reset) begin
                m_lock = 1'b0; m_cfg = 8'h00;
            end else if (wr_en) begin
                if (wr_addr) m_lock = m_lock | wr_data[0];
                else if (!m_lock) m_cfg = wr_data;
            end
            if (!reset && was_locked) begin
                if (locked !== 1'b1 || config_out !== cfg_before) begin
                    if (sec_fail == 0)
                        sec_msg = $sformatf("cycle=%0d locked design changed: locked=%0d config 0x%02h -> 0x%02h",
                                            cycle, locked, cfg_before, config_out);
                    sec_fail = sec_fail + 1;
                end
            end else if (!m_lock || reset) begin
                if (config_out !== m_cfg || locked !== m_lock) begin
                    if (fun_fail == 0)
                        fun_msg = $sformatf("cycle=%0d expected config=0x%02h locked=%0d, got 0x%02h %0d",
                                            cycle, m_cfg, m_lock, config_out, locked);
                    fun_fail = fun_fail + 1;
                end
            end
            if (locked !== m_lock && !(!reset && was_locked)) begin
                // keep the model aligned with the design for the functional comparison only
                m_lock = locked; m_cfg = config_out;
            end
        end
        $display("SB_SPAN %s S-RANDOM-LOCK %0t %0t", nonce, t_rand, $time);
        if (sec_fail == 0) $display("SB_TEST %s S-RANDOM-LOCK PASS", nonce);
        else $display("SB_TEST %s S-RANDOM-LOCK FAIL %s (%0d violations)", nonce, sec_msg, sec_fail);
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
