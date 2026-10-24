// TRUSTED formal property wrapper for the zeroize_incomplete case (SiliconBreak X).
//   P-ZEROIZE     one cycle after zeroize (no reset), key_out == 0 and key_valid == 0
//   P-NO-RESIDUE  key_valid == 0 implies key_out == 0, in every cycle after reset
module key_vault_formal (
    input wire        clk,
    input wire        reset,
    input wire        load,
    input wire [15:0] key_in,
    input wire        zeroize
);
    wire [15:0] key_out;
    wire        key_valid;

    key_vault dut (.clk(clk), .reset(reset), .load(load), .key_in(key_in), .zeroize(zeroize),
                   .key_out(key_out), .key_valid(key_valid));

    reg past_valid = 1'b0;
    reg prev_zeroize = 1'b0;

    always @(posedge clk) begin
        past_valid   <= 1'b1;
        prev_zeroize <= zeroize && !reset;
    end

    always @(*) begin
        if (!past_valid) assume (reset);
        if (past_valid && prev_zeroize) begin
            assert (key_out == 16'h0000);
            assert (!key_valid);
        end
        if (past_valid) assert (key_valid || key_out == 16'h0000);
    end
endmodule
