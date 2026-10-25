// CircuitLoop preverified library: 4-digit seven-segment display driver (Basys 3).
// digits[5k+4:5k] = {lit, hex[3:0]} for digit k (k = 0 is the rightmost, AN0).
// The digits are time-multiplexed: one digit is driven per slot of DIGIT_CYCLES
// clock cycles (100000 at 100 MHz = 1 kHz slots, a 250 Hz frame). An unlit digit
// keeps its anode off for its slot. Cathodes seg = {g,f,e,d,c,b,a} and the anodes
// are active-low, as wired on the board. Glyphs: 0-9, A, b, C, d, E, F.
// seg and an are registered together, so they always describe the same digit.
`default_nettype none
module cl_sevenseg #(
    parameter integer DIGIT_CYCLES = 100000
) (
    input  wire        clk,
    input  wire [19:0] digits,
    output reg  [6:0]  seg = 7'b1111111,
    output reg  [3:0]  an  = 4'b1111
);
    localparam integer CW   = (DIGIT_CYCLES > 1) ? $clog2(DIGIT_CYCLES) : 1;
    localparam integer LAST = DIGIT_CYCLES - 1;

    reg [CW-1:0] cnt = {CW{1'b0}};
    reg [1:0]    pos = 2'd0;

    wire [4:0] cur = (pos == 2'd0) ? digits[4:0]   :
                     (pos == 2'd1) ? digits[9:5]   :
                     (pos == 2'd2) ? digits[14:10] : digits[19:15];

    // Active-high segment pattern {g,f,e,d,c,b,a} for a hex value.
    function [6:0] glyph(input [3:0] h);
        case (h)
            4'h0: glyph = 7'b0111111;
            4'h1: glyph = 7'b0000110;
            4'h2: glyph = 7'b1011011;
            4'h3: glyph = 7'b1001111;
            4'h4: glyph = 7'b1100110;
            4'h5: glyph = 7'b1101101;
            4'h6: glyph = 7'b1111101;
            4'h7: glyph = 7'b0000111;
            4'h8: glyph = 7'b1111111;
            4'h9: glyph = 7'b1101111;
            4'hA: glyph = 7'b1110111;
            4'hB: glyph = 7'b1111100;
            4'hC: glyph = 7'b0111001;
            4'hD: glyph = 7'b1011110;
            4'hE: glyph = 7'b1111001;
            default: glyph = 7'b1110001;  // F
        endcase
    endfunction

    always @(posedge clk) begin
        if (cnt == LAST[CW-1:0]) begin
            cnt <= {CW{1'b0}};
            pos <= pos + 2'd1;
        end else begin
            cnt <= cnt + 1'b1;
        end
        an  <= cur[4] ? ~(4'b0001 << pos) : 4'b1111;
        seg <= cur[4] ? ~glyph(cur[3:0]) : 7'b1111111;
    end
endmodule
`default_nettype wire
