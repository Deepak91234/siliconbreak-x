// TRUSTED formal property wrapper for the lock_bit_clear case (SiliconBreak X).
//   P-LOCK-STICKY  locked one cycle ago and no reset since -> still locked
//   P-LOCK-FREEZE  locked one cycle ago and no reset since -> config_out unchanged
module cfg_lock_reg_formal (
    input wire       clk,
    input wire       reset,
    input wire       wr_en,
    input wire       wr_addr,
    input wire [7:0] wr_data
);
    wire [7:0] config_out;
    wire       locked;

    cfg_lock_reg dut (.clk(clk), .reset(reset), .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
                      .config_out(config_out), .locked(locked));

    reg       past_valid = 1'b0;
    reg       prev_locked = 1'b0;
    reg       prev_reset = 1'b0;
    reg [7:0] prev_config = 8'h00;

    always @(posedge clk) begin
        past_valid  <= 1'b1;
        prev_locked <= locked;
        prev_reset  <= reset;
        prev_config <= config_out;
    end

    always @(*) begin
        if (!past_valid) assume (reset);
        if (past_valid && prev_locked && !prev_reset) begin
            assert (locked);
            assert (config_out == prev_config);
        end
    end
endmodule
